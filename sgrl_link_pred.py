from pathlib import Path
from pprint import pprint
from timeit import default_timer

import gdown as gdown
import numpy as np
import torch
import shutil

import argparse
import time
import os
import sys
import os.path as osp
from shutil import copy

from gtrick.pyg import ResourceAllocation, AdamicAdar, AnchorDistance, CommonNeighbors
from ray import tune
from torch.optim import lr_scheduler
from torch_geometric import seed_everything
from torch_geometric.loader import DataLoader
from torch_geometric.nn.conv.gcn_conv import gcn_norm
from torch_geometric.profile import profileit, timeit
from torch_geometric.transforms import NormalizeFeatures, OneHotDegree
from tqdm import tqdm

from sklearn.metrics import roc_auc_score, average_precision_score
import scipy.sparse as ssp
from torch.nn import BCEWithLogitsLoss

from torch_sparse import coalesce, SparseTensor

from torch_geometric.datasets import Planetoid, AttributedGraphDataset, WikipediaNetwork, WebKB, Coauthor
from torch_geometric.data import Dataset, InMemoryDataset, Data
from torch_geometric.utils import to_undirected

from ogb.linkproppred import PygLinkPropPredDataset, Evaluator

import warnings
from scipy.sparse import SparseEfficiencyWarning

from custom_losses import auc_loss, hinge_auc_loss
from data_utils import read_label, read_edges
from models import SAGE, DGCNN, GCN, GIN, S3GRLLight, S3GRLHeavy
from n2v_prep import node_2_vec_pretrain

from profiler_utils import profile_helper
# DO NOT REMOVE AA CN PPR IMPORTS
from utils import get_pos_neg_edges, extract_enclosing_subgraphs, do_edge_split, Logger, AA, CN, PPR, calc_ratio_helper, \
    create_rw_cache, adjust_lr, file_size

warnings.simplefilter('ignore', SparseEfficiencyWarning)
warnings.simplefilter('ignore', FutureWarning)
warnings.simplefilter('ignore', UserWarning)


class SGRLDataset(InMemoryDataset):
    def __init__(self, root, data, split_edge, num_hops, percent=100, split='train',
                 use_coalesce=False, node_label='drnl', ratio_per_hop=1.0,
                 max_nodes_per_hop=None, directed=False, rw_kwargs=None, device='cpu', pairwise=False,
                 pos_pairwise=False, neg_ratio=1, use_feature=False, sign_type="", args=None):
        self.data = data
        self.split_edge = split_edge
        self.num_hops = num_hops
        self.percent = int(percent) if percent >= 1.0 else percent
        self.split = split
        self.use_coalesce = use_coalesce
        self.node_label = node_label
        self.ratio_per_hop = ratio_per_hop
        self.max_nodes_per_hop = max_nodes_per_hop
        self.directed = directed
        self.device = device
        self.N = self.data.num_nodes
        self.E = self.data.edge_index.size()[-1]
        self.sparse_adj = SparseTensor(
            row=self.data.edge_index[0].to(self.device), col=self.data.edge_index[1].to(self.device),
            value=torch.arange(self.E, device=self.device),
            sparse_sizes=(self.N, self.N))
        self.rw_kwargs = rw_kwargs
        self.pairwise = pairwise
        self.pos_pairwise = pos_pairwise
        self.neg_ratio = neg_ratio
        self.use_feature = use_feature
        self.sign_type = sign_type
        self.args = args
        super(SGRLDataset, self).__init__(root)
        if not self.rw_kwargs.get('calc_ratio', False):
            self.data, self.slices = torch.load(self.processed_paths[0])

    @property
    def processed_file_names(self):
        if self.percent == 100:
            name = 'SGRL_{}_data'.format(self.split)
        else:
            name = 'SGRL_{}_data_{}'.format(self.split, self.percent)
        name += '.pt'
        return [name]

    def process(self):
        pos_edge, neg_edge = get_pos_neg_edges(self.split, self.split_edge,
                                               self.data.edge_index,
                                               self.data.num_nodes,
                                               self.percent, neg_ratio=self.neg_ratio)

        if self.use_coalesce:  # compress mutli-edge into edge with weight
            self.data.edge_index, self.data.edge_weight = coalesce(
                self.data.edge_index, self.data.edge_weight,
                self.data.num_nodes, self.data.num_nodes)

        if 'edge_weight' in self.data:
            edge_weight = self.data.edge_weight.view(-1)
        else:
            edge_weight = torch.ones(self.data.edge_index.size(1), dtype=int)
        A = ssp.csr_matrix(
            (edge_weight, (self.data.edge_index[0], self.data.edge_index[1])),
            shape=(self.data.num_nodes, self.data.num_nodes)
        )

        if self.directed:
            A_csc = A.tocsc()
        else:
            A_csc = None

        # Extract enclosing subgraphs for pos and neg edges

        cached_pos_rws = cached_neg_rws = None
        if self.rw_kwargs.get('m') and self.args.optimize_sign and self.sign_type == "PoS":
            cached_pos_rws = create_rw_cache(self.sparse_adj, pos_edge, self.device, self.rw_kwargs['m'],
                                             self.rw_kwargs['M'])
            cached_neg_rws = create_rw_cache(self.sparse_adj, neg_edge, self.device, self.rw_kwargs['m'],
                                             self.rw_kwargs['M'])

        rw_kwargs = {
            "rw_m": self.rw_kwargs.get('m'),
            "rw_M": self.rw_kwargs.get('M'),
            "sparse_adj": self.sparse_adj,
            "edge_index": self.data.edge_index,
            "device": self.device,
            "data": self.data,
            "node_label": self.node_label,
            "cached_pos_rws": cached_pos_rws,
            "cached_neg_rws": cached_neg_rws,
        }

        sign_kwargs = {}
        powers_of_A = []
        if self.args.model == 'SIGN':
            sign_k = self.args.sign_k
            sign_type = self.sign_type
            sign_kwargs.update({
                "sign_k": sign_k,
                "use_feature": self.use_feature,
                "sign_type": sign_type,
                "optimize_sign": self.args.optimize_sign,
                "k_heuristic": self.args.k_heuristic,
                "k_node_set_strategy": self.args.k_node_set_strategy,
            })

            if not self.rw_kwargs.get('m'):
                rw_kwargs = None
            else:
                rw_kwargs.update({"sign": True})

            if sign_type == 'SoP':
                edge_index = self.data.edge_index
                num_nodes = self.data.num_nodes

                edge_index, value = gcn_norm(edge_index, edge_weight=edge_weight.to(torch.float),
                                             add_self_loops=True)
                adj_t = SparseTensor(row=edge_index[0], col=edge_index[-1], value=value,
                                     sparse_sizes=(num_nodes, num_nodes))
                if self.directed:
                    adj_t = adj_t.to_symmetric()
                print("Begin taking powers of A")
                powers_of_A = [adj_t]
                for _ in tqdm(range(2, self.args.sign_k + 1), ncols=70):
                    powers_of_A += [adj_t @ powers_of_A[-1]]

                if not sign_kwargs['optimize_sign']:
                    for index in range(len(powers_of_A)):
                        powers_of_A[index] = ssp.csr_matrix(powers_of_A[index].to_dense())

        if self.rw_kwargs.get('calc_ratio', False) and self.rw_kwargs.get('m'):
            # helps calculate the average sparsity of subgraphs in ScaLed vs. SEAL
            # only intended for ScaLed model.
            print(f"Calculating preprocessing stats for {self.split}")
            if self.args.model == "SIGN":
                raise NotImplementedError("calc_ratio not implemented for SIGN")
            calc_ratio_helper(pos_edge, neg_edge, A, self.data.x, -1, self.num_hops, self.node_label,
                              self.ratio_per_hop, self.max_nodes_per_hop, self.directed, A_csc, rw_kwargs, self.split,
                              self.args.dataset, self.args.seed)
            exit()

        verbose = True
        if not self.pairwise:
            print("Setting up Positive Subgraphs")
            pos_list = extract_enclosing_subgraphs(
                pos_edge, A, self.data.x, 1, self.num_hops, self.node_label,
                self.ratio_per_hop, self.max_nodes_per_hop, self.directed, A_csc, rw_kwargs, sign_kwargs,
                powers_of_A=powers_of_A, data=self.data, verbose=verbose)
            print("Setting up Negative Subgraphs")
            neg_list = extract_enclosing_subgraphs(
                neg_edge, A, self.data.x, 0, self.num_hops, self.node_label,
                self.ratio_per_hop, self.max_nodes_per_hop, self.directed, A_csc, rw_kwargs, sign_kwargs,
                powers_of_A=powers_of_A, data=self.data, verbose=verbose)
            torch.save(self.collate(pos_list + neg_list), self.processed_paths[0])
            del pos_list, neg_list
        else:
            if self.pos_pairwise:
                pos_list = extract_enclosing_subgraphs(
                    pos_edge, A, self.data.x, 1, self.num_hops, self.node_label,
                    self.ratio_per_hop, self.max_nodes_per_hop, self.directed, A_csc, rw_kwargs, sign_kwargs,
                    powers_of_A=powers_of_A, data=self.data, verbose=verbose)
                torch.save(self.collate(pos_list), self.processed_paths[0])
                del pos_list
            else:
                neg_list = extract_enclosing_subgraphs(
                    neg_edge, A, self.data.x, 0, self.num_hops, self.node_label,
                    self.ratio_per_hop, self.max_nodes_per_hop, self.directed, A_csc, rw_kwargs, sign_kwargs,
                    powers_of_A=powers_of_A, data=self.data, verbose=verbose)
                torch.save(self.collate(neg_list), self.processed_paths[0])
                del neg_list


class SGRLDynamicDataset(Dataset):
    def __init__(self, root, data, split_edge, num_hops, percent=100, split='train',
                 use_coalesce=False, node_label='drnl', ratio_per_hop=1.0,
                 max_nodes_per_hop=None, directed=False, rw_kwargs=None, device='cpu', pairwise=False,
                 pos_pairwise=False, neg_ratio=1, use_feature=False, sign_type="", args=None, **kwargs):
        self.data = data
        self.split_edge = split_edge
        self.num_hops = num_hops
        self.percent = percent
        self.use_coalesce = use_coalesce
        self.node_label = node_label
        self.ratio_per_hop = ratio_per_hop
        self.max_nodes_per_hop = max_nodes_per_hop
        self.directed = directed
        self.rw_kwargs = rw_kwargs
        self.device = device
        self.N = self.data.num_nodes
        self.E = self.data.edge_index.size()[-1]
        self.sparse_adj = SparseTensor(
            row=self.data.edge_index[0].to(self.device), col=self.data.edge_index[1].to(self.device),
            value=torch.arange(self.E, device=self.device),
            sparse_sizes=(self.N, self.N))
        self.pairwise = pairwise
        self.pos_pairwise = pos_pairwise
        self.neg_ratio = neg_ratio
        self.use_feature = use_feature
        self.sign_type = sign_type
        self.args = args
        super(SGRLDynamicDataset, self).__init__(root)

        pos_edge, neg_edge = get_pos_neg_edges(split, self.split_edge,
                                               self.data.edge_index,
                                               self.data.num_nodes,
                                               self.percent, neg_ratio=self.neg_ratio)
        if self.pairwise:
            if self.pos_pairwise:
                self.links = pos_edge.t().tolist()
                self.labels = [1] * pos_edge.size(1)
            else:
                self.links = neg_edge.t().tolist()
                self.labels = [0] * neg_edge.size(1)
        else:
            self.links = torch.cat([pos_edge, neg_edge], 1).t().tolist()
            self.labels = [1] * pos_edge.size(1) + [0] * neg_edge.size(1)

        self.cached_data = [0] * len(self.links)
        self.use_cache = False

        if self.use_coalesce:  # compress mutli-edge into edge with weight
            self.data.edge_index, self.data.edge_weight = coalesce(
                self.data.edge_index, self.data.edge_weight,
                self.data.num_nodes, self.data.num_nodes)

        if 'edge_weight' in self.data:
            edge_weight = self.data.edge_weight.view(-1)
        else:
            edge_weight = torch.ones(self.data.edge_index.size(1), dtype=int)
        self.A = ssp.csr_matrix(
            (edge_weight, (self.data.edge_index[0], self.data.edge_index[1])),
            shape=(self.data.num_nodes, self.data.num_nodes)
        )
        if self.directed:
            self.A_csc = self.A.tocsc()
        else:
            self.A_csc = None

        self.unique_nodes = {}
        self.cached_pos_rws = None
        self.cached_neg_rws = None
        if self.rw_kwargs.get('M'):
            print("Start caching random walk unique nodes")
            # if in dynamic ScaLed mode, need to cache the unique nodes of random walks before get() due to below error
            # RuntimeError: Cannot re-initialize CUDA in forked subprocess.
            # To use CUDA with multiprocessing, you must use the 'spawn' start method
            if self.rw_kwargs.get('m') and self.args.optimize_sign and self.sign_type == "PoS":
                # currently only cache for flows involving PoS + Optimized using the SIGN + ScaLed flow
                self.cached_pos_rws = create_rw_cache(self.sparse_adj, pos_edge, self.device, self.rw_kwargs['m'],
                                                      self.rw_kwargs['M'])
                self.cached_neg_rws = create_rw_cache(self.sparse_adj, neg_edge, self.device, self.rw_kwargs['m'],
                                                      self.rw_kwargs['M'])
            else:
                for link in self.links:
                    rw_M = self.rw_kwargs.get('M')
                    starting_nodes = []
                    [starting_nodes.extend(link) for _ in range(rw_M)]
                    start = torch.tensor(starting_nodes, dtype=torch.long, device=device)
                    rw = self.sparse_adj.random_walk(start.flatten(), self.rw_kwargs.get('m'))
                    self.unique_nodes[tuple(link)] = torch.unique(rw.flatten()).tolist()

            print("Finish caching random walk unique nodes")

        self.powers_of_A = []
        if self.args.model == 'SIGN':
            if self.sign_type == 'SoP':

                edge_index = self.data.edge_index
                num_nodes = self.data.num_nodes

                edge_index, value = gcn_norm(edge_index, edge_weight=edge_weight.to(torch.float),
                                             add_self_loops=True)
                adj_t = SparseTensor(row=edge_index[0], col=edge_index[-1], value=value,
                                     sparse_sizes=(num_nodes, num_nodes))
                if self.directed:
                    adj_t = adj_t.to_symmetric()

                print("Begin taking powers of A")
                self.powers_of_A = [adj_t]
                for _ in tqdm(range(2, self.args.sign_k + 1), ncols=70):
                    self.powers_of_A += [adj_t @ self.powers_of_A[-1]]

                if not getattr(self.args, 'optimize_sign'):
                    for index in range(len(self.powers_of_A)):
                        self.powers_of_A[index] = ssp.csr_matrix(self.powers_of_A[index].to_dense())

    def __len__(self):
        return len(self.links)

    def len(self):
        return self.__len__()

    def set_use_cache(self, flag, id):
        self.use_cache = flag
        print(f"Updated {id} loader use_cache to {flag}")

    def get(self, idx):
        if self.use_cache:
            return self.cached_data[idx]
        verbose = False
        rw_kwargs = {
            "rw_m": self.rw_kwargs.get('m'),
            "rw_M": self.rw_kwargs.get('M'),
            "sparse_adj": self.sparse_adj,
            "edge_index": self.data.edge_index,
            "device": self.device,
            "data": self.data,
            "unique_nodes": self.unique_nodes,
            "node_label": self.node_label,
            "cached_pos_rws": self.cached_pos_rws,
            "cached_neg_rws": self.cached_neg_rws,

        }
        sign_kwargs = {}
        if self.args.model == 'SIGN':
            sign_k = self.args.sign_k
            sign_type = self.sign_type
            sign_kwargs.update({
                "sign_k": sign_k,
                "use_feature": self.use_feature,
                "sign_type": sign_type,
                "optimize_sign": self.args.optimize_sign,
                "k_heuristic": self.args.k_heuristic,
                "k_node_set_strategy": self.args.k_node_set_strategy,
            })
            if not self.rw_kwargs.get('m'):
                rw_kwargs = None
            else:
                rw_kwargs.update({"sign": True})
        y = self.labels[idx]
        link_index = torch.tensor([[self.links[idx][0]], [self.links[idx][1]]])

        data = extract_enclosing_subgraphs(
            link_index, self.A, self.data.x, y, self.num_hops, self.node_label,
            self.ratio_per_hop, self.max_nodes_per_hop, self.directed, self.A_csc, rw_kwargs, sign_kwargs,
            powers_of_A=self.powers_of_A, data=self.data, verbose=verbose)[0]
        if self.args.cache_dynamic:
            self.cached_data[idx] = data

        return data


@profileit("cuda")
def profile_train(model, train_loader, optimizer, device, emb, train_dataset, args):
    # normal training with BCE logit loss with profiling enabled
    model.train()

    total_loss = 0
    pbar = tqdm(train_loader, ncols=70)
    for data in pbar:
        data = data.to(device)
        optimizer.zero_grad()
        x = data.x if args.use_feature else None
        edge_weight = data.edge_weight if args.use_edge_weight else None
        node_id = data.node_id if emb else None
        num_nodes = data.num_nodes
        if args.model == 'SIGN':
            sign_k = args.sign_k
            if args.sign_type == 'hybrid':
                sign_k = args.sign_k * args.num_hops
            if sign_k != -1:
                xs = [data.x.to(device)]
                xs += [data[f'x{i}'].to(device) for i in range(1, sign_k + 1)]
            else:
                xs = [data[f'x{args.sign_k}'].to(device)]
            operator_batch_data = [data.batch] + [data[f"x{index}_batch"] for index in range(1, args.sign_k + 1)]
            logits = model(xs, operator_batch_data)
        else:
            logits = model(num_nodes, data.z, data.edge_index, data.batch, x, edge_weight, node_id)
        loss = BCEWithLogitsLoss()(logits.view(-1), data.y.to(torch.float))
        loss.backward()
        optimizer.step()
        total_loss += loss.item() * data.num_graphs

    return total_loss / len(train_dataset)


def train_bce(model, train_loader, optimizer, device, emb, train_dataset, args, epoch):
    # normal training with BCE logit loss
    model.train()

    total_loss = 0
    pbar = tqdm(train_loader, ncols=70)
    for data in pbar:
        data = data.to(device)
        optimizer.zero_grad()
        if args.model == 'SIGN':
            sign_k = args.sign_k
            if args.sign_type == 'hybrid':
                sign_k = args.sign_k * args.num_hops
            if sign_k != -1:
                xs = [data.x.to(device)]
                xs += [data[f'x{i}'].to(device) for i in range(1, sign_k + 1)]
            else:
                xs = [data[f'x{args.sign_k}'].to(device)]
            operator_batch_data = [data.batch] + [data[f"x{index}_batch"] for index in range(1, args.sign_k + 1)]
            logits = model(xs, operator_batch_data)
        else:
            x = data.x if args.use_feature else None
            edge_weight = data.edge_weight if args.use_edge_weight else None
            node_id = data.node_id if emb else None
            num_nodes = data.num_nodes
            logits = model(num_nodes, data.z, data.edge_index, data.batch, x, edge_weight, node_id)

        loss = BCEWithLogitsLoss()(logits.view(-1), data.y.to(torch.float))
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1)
        optimizer.step()
        total_loss += loss.item() * data.num_graphs

    return total_loss / len(train_dataset)


def train_pairwise(model, train_positive_loader, train_negative_loader, optimizer, device, emb, train_dataset, args,
                   epoch):
    # pairwise training with AUC loss + many others from PLNLP paper
    model.train()

    total_loss = 0
    pbar = tqdm(list(zip(train_positive_loader, train_negative_loader)), ncols=70)

    for indx, (data, neg_data) in enumerate(pbar):
        pos_data = data.to(device)
        optimizer.zero_grad()

        pos_x = pos_data.x if args.use_feature else None
        pos_edge_weight = pos_data.edge_weight if args.use_edge_weight else None
        pos_node_id = pos_data.node_id if emb else None
        pos_num_nodes = pos_data.num_nodes
        if args.model == 'SIGN':
            if args.sign_k != -1:
                xs = [data.x.to(device)]
                xs += [data[f'x{i}'].to(device) for i in range(1, args.sign_k + 1)]
            else:
                xs = [data[f'x{args.sign_k}'].to(device)]
            operator_batch_data = [data.batch] + [data[f"x{index}_batch"] for index in range(1, args.sign_k + 1)]
            pos_logits = model(xs, operator_batch_data)
        else:
            pos_logits = model(pos_num_nodes, pos_data.z, pos_data.edge_index, data.batch, pos_x, pos_edge_weight,
                               pos_node_id)

        neg_x = neg_data.x if args.use_feature else None
        neg_edge_weight = neg_data.edge_weight if args.use_edge_weight else None
        neg_node_id = neg_data.node_id if emb else None
        neg_num_nodes = neg_data.num_nodes

        if args.model == 'SIGN':
            if args.sign_k != -1:
                xs = [neg_data.x.to(device)]
                xs += [neg_data[f'x{i}'].to(device) for i in range(1, args.sign_k + 1)]
            else:
                xs = [neg_data[f'x{args.sign_k}'].to(device)]
            operator_batch_data = [neg_data.batch] + [neg_data[f"x{index}_batch"] for index in
                                                      range(1, args.sign_k + 1)]
            neg_logits = model(xs, operator_batch_data)
        else:
            neg_logits = model(neg_num_nodes, neg_data.z, neg_data.edge_index, neg_data.batch, neg_x, neg_edge_weight,
                               neg_node_id)

        loss_fn = get_loss(args.loss_fn)
        loss = loss_fn(pos_logits, neg_logits, args.neg_ratio)

        torch.nn.utils.clip_grad_norm_(model.parameters(), 1)
        adjust_lr(optimizer, epoch / args.epochs, args.lr)

        loss.backward()
        optimizer.step()
        total_loss += loss.item() * data.num_graphs

    return total_loss / len(train_dataset)


def get_loss(loss_function):
    if loss_function == 'auc_loss':
        return auc_loss
    elif loss_function == 'hinge_auc_loss':
        return hinge_auc_loss
    else:
        raise NotImplementedError(f'Loss function {loss_function} not implemented')


@torch.no_grad()
def test(evaluator, model, val_loader, device, emb, test_loader, args):
    model.eval()

    y_pred, y_true = [], []
    for data in tqdm(val_loader, ncols=70):
        data = data.to(device)
        x = data.x if args.use_feature else None
        edge_weight = data.edge_weight if args.use_edge_weight else None
        node_id = data.node_id if emb else None
        num_nodes = data.num_nodes
        if args.model == 'SIGN':
            sign_k = args.sign_k
            if args.sign_type == 'hybrid':
                sign_k = args.sign_k * args.num_hops
            if sign_k != -1:
                xs = [data.x.to(device)]
                xs += [data[f'x{i}'].to(device) for i in range(1, sign_k + 1)]
            else:
                xs = [data[f'x{args.sign_k}'].to(device)]
            operator_batch_data = [data.batch] + [data[f"x{index}_batch"] for index in range(1, args.sign_k + 1)]
            logits = model(xs, operator_batch_data)
        else:
            logits = model(num_nodes, data.z, data.edge_index, data.batch, x, edge_weight, node_id)
        y_pred.append(logits.view(-1).cpu())
        y_true.append(data.y.view(-1).cpu().to(torch.float))
    val_pred, val_true = torch.cat(y_pred), torch.cat(y_true)
    pos_val_pred = val_pred[val_true == 1]
    neg_val_pred = val_pred[val_true == 0]

    if args.profile:
        out, time_for_inference = _get_test_auc_with_prof(args, device, emb, model, test_loader)
    else:
        time_for_inference_start = default_timer()
        out = _get_test_auc(args, device, emb, model, test_loader)
        time_for_inference_end = default_timer()
        time_for_inference = time_for_inference_end - time_for_inference_start

    neg_test_pred, pos_test_pred, test_pred, test_true = out

    if args.eval_metric == 'hits':
        results = evaluate_hits(pos_val_pred, neg_val_pred, pos_test_pred, neg_test_pred, evaluator)
    elif args.eval_metric == 'mrr':
        results = evaluate_mrr(pos_val_pred, neg_val_pred, pos_test_pred, neg_test_pred, evaluator)
    elif args.eval_metric == 'rocauc':
        results = evaluate_ogb_rocauc(pos_val_pred, neg_val_pred, pos_test_pred, neg_test_pred, evaluator)
    elif args.eval_metric == 'auc':
        results = evaluate_auc(val_pred, val_true, test_pred, test_true)

    return results, time_for_inference


@timeit()
@torch.no_grad()
def _get_test_auc_with_prof(args, device, emb, model, test_loader):
    y_pred, y_true = [], []
    for data in tqdm(test_loader, ncols=70):
        data = data.to(device)
        x = data.x if args.use_feature else None
        edge_weight = data.edge_weight if args.use_edge_weight else None
        node_id = data.node_id if emb else None
        num_nodes = data.num_nodes
        if args.model == 'SIGN':
            sign_k = args.sign_k
            if args.sign_type == 'hybrid':
                sign_k = args.sign_k * args.num_hops
            if sign_k != -1:
                xs = [data.x.to(device)]
                xs += [data[f'x{i}'].to(device) for i in range(1, sign_k + 1)]
            else:
                xs = [data[f'x{args.sign_k}'].to(device)]
            operator_batch_data = [data.batch] + [data[f"x{index}_batch"] for index in range(1, args.sign_k + 1)]
            logits = model(xs, operator_batch_data)
        else:
            logits = model(num_nodes, data.z, data.edge_index, data.batch, x, edge_weight, node_id)
        y_pred.append(logits.view(-1).cpu())
        y_true.append(data.y.view(-1).cpu().to(torch.float))
    test_pred, test_true = torch.cat(y_pred), torch.cat(y_true)
    pos_test_pred = test_pred[test_true == 1]
    neg_test_pred = test_pred[test_true == 0]
    return neg_test_pred, pos_test_pred, test_pred, test_true


@torch.no_grad()
def _get_test_auc(args, device, emb, model, test_loader):
    y_pred, y_true = [], []
    for data in tqdm(test_loader, ncols=70):
        data = data.to(device)
        x = data.x if args.use_feature else None
        edge_weight = data.edge_weight if args.use_edge_weight else None
        node_id = data.node_id if emb else None
        num_nodes = data.num_nodes
        if args.model == 'SIGN':
            sign_k = args.sign_k
            if args.sign_type == 'hybrid':
                sign_k = args.sign_k * args.num_hops
            if sign_k != -1:
                xs = [data.x.to(device)]
                xs += [data[f'x{i}'].to(device) for i in range(1, sign_k + 1)]
            else:
                xs = [data[f'x{args.sign_k}'].to(device)]
            operator_batch_data = [data.batch] + [data[f"x{index}_batch"] for index in range(1, args.sign_k + 1)]
            logits = model(xs, operator_batch_data)
        else:
            logits = model(num_nodes, data.z, data.edge_index, data.batch, x, edge_weight, node_id)
        y_pred.append(logits.view(-1).cpu())
        y_true.append(data.y.view(-1).cpu().to(torch.float))
    test_pred, test_true = torch.cat(y_pred), torch.cat(y_true)
    pos_test_pred = test_pred[test_true == 1]
    neg_test_pred = test_pred[test_true == 0]
    return neg_test_pred, pos_test_pred, test_pred, test_true


@torch.no_grad()
def test_multiple_models(models, val_loader, device, emb, test_loader, evaluator, args):
    raise NotImplementedError("This is untested")
    for m in models:
        m.eval()

    y_pred, y_true = [[] for _ in range(len(models))], [[] for _ in range(len(models))]
    for data in tqdm(val_loader, ncols=70):
        data = data.to(device)
        x = data.x if args.use_feature else None
        edge_weight = data.edge_weight if args.use_edge_weight else None
        node_id = data.node_id if emb else None
        for i, m in enumerate(models):
            logits = m(data.z, data.edge_index, data.batch, x, edge_weight, node_id)
            y_pred[i].append(logits.view(-1).cpu())
            y_true[i].append(data.y.view(-1).cpu().to(torch.float))
    val_pred = [torch.cat(y_pred[i]) for i in range(len(models))]
    val_true = [torch.cat(y_true[i]) for i in range(len(models))]
    pos_val_pred = [val_pred[i][val_true[i] == 1] for i in range(len(models))]
    neg_val_pred = [val_pred[i][val_true[i] == 0] for i in range(len(models))]

    y_pred, y_true = [[] for _ in range(len(models))], [[] for _ in range(len(models))]
    for data in tqdm(test_loader, ncols=70):
        data = data.to(device)
        x = data.x if args.use_feature else None
        edge_weight = data.edge_weight if args.use_edge_weight else None
        node_id = data.node_id if emb else None
        for i, m in enumerate(models):
            logits = m(data.z, data.edge_index, data.batch, x, edge_weight, node_id)
            y_pred[i].append(logits.view(-1).cpu())
            y_true[i].append(data.y.view(-1).cpu().to(torch.float))
    test_pred = [torch.cat(y_pred[i]) for i in range(len(models))]
    test_true = [torch.cat(y_true[i]) for i in range(len(models))]
    pos_test_pred = [test_pred[i][test_true[i] == 1] for i in range(len(models))]
    neg_test_pred = [test_pred[i][test_true[i] == 0] for i in range(len(models))]

    Results = []
    for i in range(len(models)):
        if args.eval_metric == 'hits':
            Results.append(evaluate_hits(pos_val_pred[i], neg_val_pred[i],
                                         pos_test_pred[i], neg_test_pred[i]))
        elif args.eval_metric == 'mrr':
            Results.append(evaluate_mrr(pos_val_pred[i], neg_val_pred[i],
                                        pos_test_pred[i], neg_test_pred[i], evaluator))
        elif args.eval_metric == 'rocauc':
            Results.append(evaluate_ogb_rocauc(pos_val_pred[i], neg_val_pred[i],
                                               pos_test_pred[i], neg_test_pred[i], evaluator))
        elif args.eval_metric == 'auc':
            Results.append(evaluate_auc(val_pred[i], val_true[i],
                                        test_pred[i], test_pred[i]))
    return Results


def evaluate_hits(pos_val_pred, neg_val_pred, pos_test_pred, neg_test_pred, evaluator):
    results = {}
    for K in [20, 50, 100]:
        evaluator.K = K
        valid_hits = evaluator.eval({
            'y_pred_pos': pos_val_pred,
            'y_pred_neg': neg_val_pred,
        })[f'hits@{K}']
        test_hits = evaluator.eval({
            'y_pred_pos': pos_test_pred,
            'y_pred_neg': neg_test_pred,
        })[f'hits@{K}']

        results[f'Hits@{K}'] = (valid_hits, test_hits)

    return results


def evaluate_mrr(pos_val_pred, neg_val_pred, pos_test_pred, neg_test_pred, evaluator):
    neg_val_pred = neg_val_pred.view(pos_val_pred.shape[0], -1)
    neg_test_pred = neg_test_pred.view(pos_test_pred.shape[0], -1)
    results = {}
    valid_mrr = evaluator.eval({
        'y_pred_pos': pos_val_pred,
        'y_pred_neg': neg_val_pred,
    })['mrr_list'].mean().item()

    test_mrr = evaluator.eval({
        'y_pred_pos': pos_test_pred,
        'y_pred_neg': neg_test_pred,
    })['mrr_list'].mean().item()

    results['MRR'] = (valid_mrr, test_mrr)

    return results


def evaluate_ogb_rocauc(pos_val_pred, neg_val_pred, pos_test_pred, neg_test_pred, evaluator):
    valid_rocauc = evaluator.eval({
        'y_pred_pos': pos_val_pred,
        'y_pred_neg': neg_val_pred,
    })[f'rocauc']

    test_rocauc = evaluator.eval({
        'y_pred_pos': pos_test_pred,
        'y_pred_neg': neg_test_pred,
    })[f'rocauc']

    results = {}
    results['rocauc'] = (valid_rocauc, test_rocauc)
    return results


def evaluate_auc(val_pred, val_true, test_pred, test_true):
    # this also evaluates AP, but the function is not renamed as such
    valid_auc = roc_auc_score(val_true, val_pred)
    test_auc = roc_auc_score(test_true, test_pred)

    valid_ap = average_precision_score(val_true, val_pred)
    test_ap = average_precision_score(test_true, test_pred)

    results = {}

    results['AUC'] = (valid_auc, test_auc)
    results['AP'] = (valid_ap, test_ap)

    return results


def run_sgrl_learning_with_ray(config, hyper_param_class, device):
    args = hyper_param_class
    print(config)
    if config:
        print("Using override values for hypertuning")
        # override defaults for each hyperparam tuning run
        args.hidden_channels = config['hidden_channels']
        args.batch_size = config['batch_size']
        args.num_hops = config['num_hops']
        args.lr = config['lr']
        args.dropout = config['dropout']
        args.sign_k = config['sign_k']
        args.n2v_dim = config['n2v_dim']
        args.k_heuristic = config['k_heuristic']

    run_sgrl_learning(args, device, hypertuning=True)


def run_sgrl_learning(args, device, hypertuning=False):
    print(f"Current arguments accepted are: {args}")
    if args.save_appendix == '':
        args.save_appendix = '_' + time.strftime("%Y%m%d%H%M%S") + f'_seed{args.seed}'
        if args.m and args.M:
            args.save_appendix += f'_m{args.m}_M{args.M}_dropedge{args.dropedge}_seed{args.seed}'

    if args.data_appendix == '':
        if args.m and args.M:
            args.data_appendix = f'_m{args.m}_M{args.M}_dropedge{args.dropedge}_seed{args.seed}'
        else:
            args.data_appendix = '_h{}_{}_rph{}_seed{}'.format(
                args.num_hops, args.node_label, ''.join(str(args.ratio_per_hop).split('.')), args.seed)
            if args.max_nodes_per_hop is not None:
                args.data_appendix += '_mnph{}'.format(args.max_nodes_per_hop)
        if args.use_valedges_as_input:
            args.data_appendix += '_uvai'

    args.res_dir = os.path.join('results/{}{}'.format(args.dataset, args.save_appendix))
    print('Results will be saved in ' + args.res_dir)
    if not os.path.exists(args.res_dir):
        os.makedirs(args.res_dir)
    if not args.keep_old:
        # Backup python files.
        copy('sgrl_link_pred.py', args.res_dir)
        copy('utils.py', args.res_dir)
    log_file = os.path.join(args.res_dir, 'log.txt')
    # Save command line input.
    cmd_input = 'python ' + ' '.join(sys.argv) + '\n'
    with open(os.path.join(args.res_dir, 'cmd_input.txt'), 'a') as f:
        f.write(cmd_input)
    print('Command line input: ' + cmd_input + ' is saved.')
    with open(log_file, 'a') as f:
        f.write('\n' + cmd_input)

    # SGRL Dataset prep + Training Flow
    if args.dataset.startswith('ogbl'):
        dataset = PygLinkPropPredDataset(name=args.dataset)
        split_edge = dataset.get_edge_split()
        if args.dataset == 'ogbl-ppa':
            dataset.data.x = dataset.data.x.type(torch.FloatTensor)
        data = dataset[0]

    elif args.dataset.startswith('attributed'):
        dataset_name = args.dataset.split('-')[-1]
        path = osp.join('dataset', dataset_name)
        dataset = AttributedGraphDataset(path, dataset_name)
        split_edge = do_edge_split(dataset, args.fast_split, val_ratio=args.split_val_ratio,
                                   test_ratio=args.split_test_ratio, neg_ratio=args.neg_ratio)
        data = dataset[0]
        data.edge_index = split_edge['train']['edge'].t()

    elif args.dataset in ['Cora', 'Pubmed', 'CiteSeer']:
        path = osp.join('dataset', args.dataset)
        dataset = Planetoid(path, args.dataset)
        split_edge = do_edge_split(dataset, args.fast_split, val_ratio=args.split_val_ratio,
                                   test_ratio=args.split_test_ratio, neg_ratio=args.neg_ratio)
        data = dataset[0]
        data.edge_index = split_edge['train']['edge'].t()
        import networkx as nx
        G = nx.Graph()
        G.add_edges_from(data.edge_index.T.detach().numpy())
    elif args.dataset in ['USAir', 'NS', 'Power', 'Celegans', 'Router', 'PB', 'Ecoli', 'Yeast']:
        # We consume the dataset split index as well
        if os.path.exists('data'):
            file_name = os.path.join('data', 'link_prediction', args.dataset.lower())
        else:
            # we consume user path
            file_name = os.path.join(str(Path.home()), 'S3GRL', 'data', 'link_prediction', args.dataset.lower())
            if not os.path.exists(file_name):
                raise FileNotFoundError("Check your file path is correct")
        node_id_mapping = read_label(file_name)
        edges = read_edges(file_name, node_id_mapping)

        import networkx as nx
        G = nx.Graph(edges)
        edges_coo = torch.tensor(edges, dtype=torch.long).t().contiguous()
        data = Data(edge_index=edges_coo.view(2, -1))
        data.edge_index = to_undirected(data.edge_index)
        data.num_nodes = torch.max(data.edge_index) + 1

        split_edge = do_edge_split(data, args.fast_split, val_ratio=args.split_val_ratio,
                                   test_ratio=args.split_test_ratio, neg_ratio=args.neg_ratio, data_passed=True)
        data.edge_index = split_edge['train']['edge'].t()

        # backward compatibility
        class DummyDataset:
            def __init__(self, root):
                self.root = root
                self.num_features = 0

            def __repr__(self):
                return args.dataset

            def __len__(self):
                return 1

        dataset = DummyDataset(root=f'dataset/{args.dataset}/SGRLDataset_{args.dataset}')
        print("Finish reading from file")
    elif args.dataset in ['chameleon', 'crocodile', 'squirrel']:
        path = osp.join('dataset', args.dataset)
        dataset = WikipediaNetwork(path, args.dataset)
        split_edge = do_edge_split(dataset, args.fast_split, val_ratio=args.split_val_ratio,
                                   test_ratio=args.split_test_ratio, neg_ratio=args.neg_ratio)
        data = dataset[0]
        data.edge_index = split_edge['train']['edge'].t()
        import networkx as nx
        G = nx.Graph()
        G.add_edges_from(data.edge_index.T.detach().numpy())
    elif args.dataset in ['Cornell', 'Texas', 'Wisconsin']:
        path = osp.join('dataset', args.dataset)
        dataset = WebKB(path, args.dataset)
        split_edge = do_edge_split(dataset, args.fast_split, val_ratio=args.split_val_ratio,
                                   test_ratio=args.split_test_ratio, neg_ratio=args.neg_ratio)
        data = dataset[0]
        data.edge_index = split_edge['train']['edge'].t()
        import networkx as nx
        G = nx.Graph()
        G.add_edges_from(data.edge_index.T.detach().numpy())
    elif args.dataset in ['CS', 'Physics']:
        path = osp.join('dataset', args.dataset)
        dataset = Coauthor(path, args.dataset)
        split_edge = do_edge_split(dataset, args.fast_split, val_ratio=args.split_val_ratio,
                                   test_ratio=args.split_test_ratio, neg_ratio=args.neg_ratio)
        data = dataset[0]
        data.edge_index = split_edge['train']['edge'].t()
        import networkx as nx
        G = nx.Graph()
        G.add_edges_from(data.edge_index.T.detach().numpy())
    else:
        raise NotImplementedError(f'dataset {args.dataset} is not yet supported.')

    max_z = 1000  # set a large max_z so that every z has embeddings to look up

    if args.dataset_stats:
        if args.dataset in ['USAir', 'NS', 'Power', 'Celegans', 'Router', 'PB', 'Ecoli', 'Yeast']:
            print(f'Dataset: {dataset}:')
            print('======================')
            print(f'Number of graphs: {len(dataset)}')
            print(f'Number of features: {dataset.num_features}')
            print(f'Number of nodes: {G.number_of_nodes()}')
            print(f'Number of edges: {G.number_of_edges()}')
            degrees = [x[1] for x in G.degree]
            print(f'Average node degree: {sum(degrees) / len(G.nodes):.2f}')
            print(f'Average clustering coeffiecient: {nx.average_clustering(G)}')
            print(f'Is undirected: {data.is_undirected()}')
            exit()
        else:
            print(f'Dataset: {dataset}:')
            print('======================')
            print(f'Number of graphs: {len(dataset)}')
            print(f'Number of features: {dataset.num_features}')
            print(f'Number of nodes: {data.num_nodes}')
            print(f'Number of edges: {G.number_of_edges()}')
            print(f'Average node degree: {data.num_edges / data.num_nodes:.2f}')
            print(f'Average clustering coeffiecient: {nx.average_clustering(G)}')
            print(f'Is undirected: {data.is_undirected()}')
            exit()

    time_for_prep_start = default_timer()
    init_features = args.init_features

    if args.dataset.startswith('ogbl-citation'):
        args.eval_metric = 'mrr'
        directed = True
    elif args.dataset.startswith('ogbl-vessel'):
        args.eval_metric = 'rocauc'
        directed = False
    elif args.dataset.startswith('ogbl'):
        args.eval_metric = 'hits'
        directed = False
    else:  # assume other datasets are undirected
        args.eval_metric = 'auc'
        directed = False

    if args.dataset == 'ogbl-collab' and args.split_by_year:
        # Taken from https://github.com/yao8839836/ogb_report/blob/main/plnlp_sign.py
        # filters training edges to edge_year >= 2010
        if hasattr(data, 'edge_year'):
            print("Filtering ogbl-collab training set to >= 2010 year")
            selected_year_index = torch.reshape(
                (split_edge['train']['year'] >= 2010).nonzero(as_tuple=False), (-1,))
            split_edge['train']['edge'] = split_edge['train']['edge'][selected_year_index]
            split_edge['train']['weight'] = split_edge['train']['weight'][selected_year_index]
            split_edge['train']['year'] = split_edge['train']['year'][selected_year_index]
            train_edge_index = split_edge['train']['edge'].t()
            # create adjacency matrix
            new_edges = to_undirected(train_edge_index, split_edge['train']['weight'])  # , reduce='add'
            new_edge_index, new_edge_weight = new_edges[0], new_edges[1]
            data.edge_weight = new_edge_weight.to(torch.float32)
            data.edge_index = new_edge_index

    if args.use_valedges_as_input:
        print("Adding validation edges to training edges")
        val_edge_index = split_edge['valid']['edge'].t()
        if not directed:
            val_edge_index = to_undirected(val_edge_index)
        data.edge_index = torch.cat([data.edge_index, val_edge_index], dim=-1)
        split_edge['train']['edge'] = data.edge_index.t()
        try:
            if torch.any(data.edge_weight):
                val_edge_weight = torch.ones([val_edge_index.size(1), 1], dtype=int)
                data.edge_weight = torch.cat([data.edge_weight.reshape(data.edge_weight.shape[0], 1), val_edge_weight],
                                             0)
        except Exception as e:
            print(str(e), "passing edge weight setting")

    n2v_params = 0
    if init_features:
        print(f"Init features using: {init_features}")

    if init_features == "degree_full":
        import torch.nn.functional as F
        from torch_geometric.utils import degree
        idx, x = data.edge_index[0], data.x
        deg = degree(idx, data.num_nodes, dtype=torch.long)
        deg = F.one_hot(deg, num_classes=max(deg) + 1).to(torch.float)
        data.x = deg
    elif init_features == "degree":
        one_hot_deg = OneHotDegree(max_degree=128, cat=True)
        data.x = one_hot_deg(data).x
    elif init_features == "ones":
        data.x = torch.ones(size=(data.num_nodes, args.hidden_channels))
    elif init_features == "zeros":
        data.x = torch.zeros(size=(data.num_nodes, args.hidden_channels))
    elif init_features == "eye":
        data.x = torch.eye(data.num_nodes)
    elif init_features == "n2v":
        extra_identifier = ''
        if args.model == "SIGN":
            extra_identifier = f"{args.k_heuristic}{args.sign_type}{args.hidden_channels}{args.num_hops}"
        print(f'Running on device: {device}')
        data.x, n2v_params = node_2_vec_pretrain(args.dataset, data.edge_index, data.num_nodes, args.n2v_dim, args.seed,
                                                 device,
                                                 args.epochs, hypertuning, extra_identifier)
    elif init_features == "distance_matrix":
        # Taken from the authors of GDNN: https://github.com/zhf3564859793/GDNN
        import networkx as nx
        import torch_geometric
        nx_data = torch_geometric.utils.to_networkx(data)
        nx_data = nx_data.to_undirected()

        # Distance Matrix
        def distance_encoding(x, y):
            distance = len(nx.shortest_path(nx_data, source=x, target=y))
            return distance

        anchors = 512  # num anchors is currently hardcoded to 512
        anchors_sampled = np.random.choice(data.num_nodes, anchors, replace=False)

        distance_feature = []

        print("Generating distance features to anchor nodes")
        for x in tqdm(range(0, data.num_nodes), ncols=70):
            distance_feature.append([])
            for y in anchors_sampled:
                dis = distance_encoding(x, y)
                distance_feature[x].append(dis)

        data.x = torch.tensor(distance_feature, dtype=torch.float)

    init_representation = args.init_representation
    if init_representation:
        print(f"Init representation using: {init_representation} model")
        from baselines.vgae import run_vgae
        original_hidden_dims = args.hidden_channels
        args.embedding_dim = args.hidden_channels
        args.hidden_channels = args.hidden_channels // 2
        # 64 -> 32 (output)
        test_and_val = [split_edge['test']['edge'].T, split_edge['test']['edge_neg'].T, split_edge['valid']['edge'].T,
                        split_edge['valid']['edge_neg'].T]
        edge_index = split_edge['train']['edge'].T
        x = data.x
        if init_representation in ['GAE', 'VGAE', 'ARGVA']:
            _, data.x = run_vgae(edge_index=edge_index, x=x, test_and_val=test_and_val, model=init_representation,
                                 args=args)
            args.hidden_channels = original_hidden_dims
        elif init_representation == 'GIC':
            args.par_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ''))
            sys.path.append('%s/Software/GIC/' % args.par_dir)
            from GICEmbs import CalGIC
            args.data_name = args.dataset
            _, data.x = CalGIC(edge_index=edge_index, features=x, dataset=args.dataset, test_and_val=test_and_val,
                               args=args)
            args.hidden_channels = original_hidden_dims
        else:
            raise NotImplementedError(f"init_representation: {init_representation} not supported.")

    if args.dataset == 'ogbl-ddi':
        # https://github.com/chuanqichen/cs224w
        from aug_helper import get_features
        extra_feats = get_features(data.num_nodes, data)
        if init_features:
            data.x = torch.cat([data.x, extra_feats], dim=-1)
        else:
            data.x = extra_feats
        print(f"Adding custom features to ogbl-ddi. Total ogbl-ddi feats is {data.x.shape}")

    augment_ppa = False  # this did not seem to improve the results for ppa, hence False
    if args.dataset == 'ogbl-ppa' and augment_ppa:
        # https://github.com/lustoo/OGB_link_prediction
        from aug_helper import resource_allocation
        adj_matrix = ssp.csr_matrix(
            (torch.ones(data.edge_index.size(1), dtype=int), (data.edge_index[0], data.edge_index[1])),
            shape=(data.num_nodes, data.num_nodes)
        )
        link_list = data.edge_index
        data.edge_weight = resource_allocation(adj_matrix, link_list)

    if args.dataset == 'ogbl-vessel':
        # https://github.com/snap-stanford/ogb/blob/master/examples/linkproppred/vessel/node2vec.py
        pretrained_file = "Emb/pretrained_n2v_ogbl_vessel.pt"

        if not os.path.isfile(pretrained_file):
            if not os.path.exists("Emb/"):
                os.makedirs("Emb/")
            url = "https://drive.google.com/uc?id=1YD7U_Umi8dsgejnI9r0rtfWq0iMaTr9A"
            gdown.download(url, pretrained_file, quiet=False)

        data.x = torch.cat([data.x, torch.load(pretrained_file, map_location=torch.device('cpu'))],
                           dim=-1)
        print(f"Concat pretrained n2v features to ogbl-vessel. Total ogbl-vessel feats is {data.x.shape}")

    if args.normalize_feats:
        print("Normalizing dataset x")
        norm = NormalizeFeatures()
        transformed_data = norm(data)
        data.x = transformed_data.x

    if args.edge_feature:
        # Use gtrick library to encode edge features
        edim = 1
        if args.edge_feature == 'cn':
            ef = CommonNeighbors(data.edge_index, batch_size=1024)
        elif args.edge_feature == 'ra':
            ef = ResourceAllocation(data.edge_index, batch_size=1024)
        elif args.edge_feature == 'aa':
            ef = AdamicAdar(data.edge_index, batch_size=1024)
        elif args.edge_feature == 'ad':
            ef = AnchorDistance(data, 3, 500, 200)
            edim = 3
        data.edge_weight = ef(edges=data.edge_index.t())
        if edim > 1:
            data.edge_weight = torch.mean(data.edge_weight, dim=-1)

    evaluator = None
    if args.dataset.startswith('ogbl'):
        evaluator = Evaluator(name=args.dataset)
    elif args.dataset.lower() in ['cora', 'pubmed', 'citeseer']:
        evaluator = Evaluator('ogbl-collab')
        evaluator.K = 100
        args.eval_metric = 'hits'

    if args.eval_metric == 'hits':
        loggers = {
            'Hits@20': Logger(args.runs, args),
            'Hits@50': Logger(args.runs, args),
            'Hits@100': Logger(args.runs, args),
        }
    elif args.eval_metric == 'mrr':
        loggers = {
            'MRR': Logger(args.runs, args),
        }
    elif args.eval_metric == 'rocauc':
        loggers = {
            'rocauc': Logger(args.runs, args),
        }
    elif args.eval_metric == 'auc':
        loggers = {
            'AUC': Logger(args.runs, args),
            'AP': Logger(args.runs, args)
        }

    if args.use_heuristic:
        # Test link prediction heuristics.
        num_nodes = data.num_nodes
        if 'edge_weight' in data:
            edge_weight = data.edge_weight.view(-1)
        else:
            edge_weight = torch.ones(data.edge_index.size(1), dtype=int)

        A = ssp.csr_matrix((edge_weight, (data.edge_index[0], data.edge_index[1])),
                           shape=(num_nodes, num_nodes))

        pos_val_edge, neg_val_edge = get_pos_neg_edges('valid', split_edge,
                                                       data.edge_index,
                                                       data.num_nodes, neg_ratio=args.neg_ratio)
        pos_test_edge, neg_test_edge = get_pos_neg_edges('test', split_edge,
                                                         data.edge_index,
                                                         data.num_nodes, neg_ratio=args.neg_ratio)
        pos_val_pred, pos_val_edge = eval(args.use_heuristic)(A, pos_val_edge)
        neg_val_pred, neg_val_edge = eval(args.use_heuristic)(A, neg_val_edge)
        pos_test_pred, pos_test_edge = eval(args.use_heuristic)(A, pos_test_edge)
        neg_test_pred, neg_test_edge = eval(args.use_heuristic)(A, neg_test_edge)

        if args.eval_metric == 'hits':
            results = evaluate_hits(pos_val_pred, neg_val_pred, pos_test_pred, neg_test_pred, evaluator)
        elif args.eval_metric == 'mrr':
            results = evaluate_mrr(pos_val_pred, neg_val_pred, pos_test_pred, neg_test_pred, evaluator)
        elif args.eval_metric == 'auc':
            val_pred = torch.cat([pos_val_pred, neg_val_pred])
            val_true = torch.cat([torch.ones(pos_val_pred.size(0), dtype=int),
                                  torch.zeros(neg_val_pred.size(0), dtype=int)])
            test_pred = torch.cat([pos_test_pred, neg_test_pred])
            test_true = torch.cat([torch.ones(pos_test_pred.size(0), dtype=int),
                                   torch.zeros(neg_test_pred.size(0), dtype=int)])
            results = evaluate_auc(val_pred, val_true, test_pred, test_true)
        elif args.eval_metric == 'rocauc':
            results = evaluate_ogb_rocauc(pos_val_pred, neg_val_pred, pos_test_pred, neg_test_pred)

        for key, result in results.items():
            loggers[key].add_result(0, result)
        for key in loggers.keys():
            print(key)
            loggers[key].print_statistics()
            with open(log_file, 'a') as f:
                print(key, file=f)
                loggers[key].print_statistics(f=f)

        return loggers['AUC'].results[0][0][-1]

    # SGRL methods including but not limited to SEAL.
    path = dataset.root + '_sgrl{}'.format(args.data_appendix)
    use_coalesce = True if args.dataset == 'ogbl-collab' else False
    if not args.dynamic_train and not args.dynamic_val and not args.dynamic_test:
        args.num_workers = 0

    rw_kwargs = {}
    if args.m and args.M:
        rw_kwargs = {
            "m": args.m,
            "M": args.M
        }
    if args.calc_ratio:
        rw_kwargs.update({'calc_ratio': True})

    if not any([args.train_gae, args.train_mf, args.train_n2v]):
        print("Setting up Train data")
        dataset_class = 'SGRLDynamicDataset' if args.dynamic_train else 'SGRLDataset'
        if not args.pairwise:
            train_dataset = eval(dataset_class)(
                path,
                data,
                split_edge,
                num_hops=args.num_hops,
                percent=args.train_percent,
                split='train',
                use_coalesce=use_coalesce,
                node_label=args.node_label,
                ratio_per_hop=args.ratio_per_hop,
                max_nodes_per_hop=args.max_nodes_per_hop,
                directed=directed,
                rw_kwargs=rw_kwargs,
                device=device,
                neg_ratio=args.neg_ratio,
                use_feature=args.use_feature,
                sign_type=args.sign_type,
                args=args,
            )
        else:
            pos_path = f'{path}_pos_edges'
            train_positive_dataset = eval(dataset_class)(
                pos_path,
                data,
                split_edge,
                num_hops=args.num_hops,
                percent=args.train_percent,
                split='train',
                use_coalesce=use_coalesce,
                node_label=args.node_label,
                ratio_per_hop=args.ratio_per_hop,
                max_nodes_per_hop=args.max_nodes_per_hop,
                directed=directed,
                rw_kwargs=rw_kwargs,
                device=device,
                pairwise=args.pairwise,
                pos_pairwise=True,
                neg_ratio=args.neg_ratio,
                use_feature=args.use_feature,
                sign_type=args.sign_type,
                args=args,
            )
            neg_path = f'{path}_neg_edges'
            train_negative_dataset = eval(dataset_class)(
                neg_path,
                data,
                split_edge,
                num_hops=args.num_hops,
                percent=args.train_percent,
                split='train',
                use_coalesce=use_coalesce,
                node_label=args.node_label,
                ratio_per_hop=args.ratio_per_hop,
                max_nodes_per_hop=args.max_nodes_per_hop,
                directed=directed,
                rw_kwargs=rw_kwargs,
                device=device,
                pairwise=args.pairwise,
                pos_pairwise=False,
                neg_ratio=args.neg_ratio,
                use_feature=args.use_feature,
                sign_type=args.sign_type,
                args=args,
            )

    if not any([args.train_gae, args.train_mf, args.train_n2v]):
        print("Setting up Val data")
        dataset_class = 'SGRLDynamicDataset' if args.dynamic_val else 'SGRLDataset'
        val_dataset = eval(dataset_class)(
            path,
            data,
            split_edge,
            num_hops=args.num_hops,
            percent=args.val_percent,
            split='valid',
            use_coalesce=use_coalesce,
            node_label=args.node_label,
            ratio_per_hop=args.ratio_per_hop,
            max_nodes_per_hop=args.max_nodes_per_hop,
            directed=directed,
            rw_kwargs=rw_kwargs,
            device=device,
            use_feature=args.use_feature,
            sign_type=args.sign_type,
            args=args,
        )
        print("Setting up Test data")
        dataset_class = 'SGRLDynamicDataset' if args.dynamic_test else 'SGRLDataset'
        test_dataset = eval(dataset_class)(
            path,
            data,
            split_edge,
            num_hops=args.num_hops,
            percent=args.test_percent,
            split='test',
            use_coalesce=use_coalesce,
            node_label=args.node_label,
            ratio_per_hop=args.ratio_per_hop,
            max_nodes_per_hop=args.max_nodes_per_hop,
            directed=directed,
            rw_kwargs=rw_kwargs,
            device=device,
            use_feature=args.use_feature,
            sign_type=args.sign_type,
            args=args,
        )

    if args.calc_ratio:
        print("Finished calculating ratio of datasets.")
        exit()

    time_for_prep_end = default_timer()
    total_prep_time = time_for_prep_end - time_for_prep_start
    print(f"Total Prep time: {total_prep_time} sec")

    if args.size_only:
        train_file = train_dataset.processed_file_names
        test_file = test_dataset.processed_file_names
        val_file = val_dataset.processed_file_names

        train_file_size = file_size(os.path.join(path, 'processed', train_file[0]))
        test_file_size = file_size(os.path.join(path, 'processed', test_file[0]))
        valid_file_size = file_size(os.path.join(path, 'processed', val_file[0]))

        size_details = {
            "Train Size": train_file_size,
            "Test Size": test_file_size,
            "Val Size": valid_file_size,
        }

        return size_details

    follow_batch = None
    if args.model == "SIGN":
        follow_batch = [f'x{index}' for index in range(1, args.sign_k + 1)]

    if not any([args.train_gae, args.train_mf, args.train_n2v]):
        if args.pairwise:
            train_pos_loader = DataLoader(train_positive_dataset, batch_size=args.batch_size,
                                          shuffle=True, num_workers=args.num_workers, follow_batch=follow_batch)
            train_neg_loader = DataLoader(train_negative_dataset, batch_size=args.batch_size * args.neg_ratio,
                                          shuffle=True, num_workers=args.num_workers, follow_batch=follow_batch)
        else:
            train_loader = DataLoader(train_dataset, batch_size=args.batch_size,
                                      shuffle=True, num_workers=args.num_workers, follow_batch=follow_batch)
        val_loader = DataLoader(val_dataset, batch_size=args.batch_size,
                                num_workers=args.num_workers, follow_batch=follow_batch)
        test_loader = DataLoader(test_dataset, batch_size=args.batch_size,
                                 num_workers=args.num_workers, follow_batch=follow_batch)

    if args.train_node_embedding:
        emb = torch.nn.Embedding(data.num_nodes, args.hidden_channels).to(device)
    elif args.pretrained_node_embedding:
        weight = torch.load(args.pretrained_node_embedding)
        emb = torch.nn.Embedding.from_pretrained(weight)
        emb.weight.requires_grad = False
    else:
        emb = None

    seed_everything(args.seed)  # reset rng for model weights
    for run in range(args.runs):
        if args.pairwise:
            train_dataset = train_positive_dataset
        if args.train_gae:
            raise NotImplementedError("No longer supported through SGRL learning script.")
        if args.train_n2v:
            raise NotImplementedError("No longer supported through SGRL learning script.")
        if args.train_mf:
            raise NotImplementedError("No longer supported through SGRL learning script.")
        if args.model == 'DGCNN':
            model = DGCNN(args.hidden_channels, args.num_layers, max_z, args.sortpool_k,
                          train_dataset, args.dynamic_train, use_feature=args.use_feature,
                          node_embedding=emb, dropedge=args.dropedge).to(device)
        elif args.model == 'SAGE':
            model = SAGE(args.hidden_channels, args.num_layers, max_z, train_dataset,
                         args.use_feature, node_embedding=emb, dropedge=args.dropedge).to(device)
        elif args.model == 'GCN':
            model = GCN(args.hidden_channels, args.num_layers, max_z, train_dataset,
                        args.use_feature, node_embedding=emb, dropedge=args.dropedge).to(device)
        elif args.model == 'GIN':
            model = GIN(args.hidden_channels, args.num_layers, max_z, train_dataset,
                        args.use_feature, node_embedding=emb).to(device)
        elif args.model == "SIGN":
            sign_k = args.sign_k
            if args.sign_type == 'hybrid':
                sign_k = args.sign_k * args.num_hops
            if args.dataset in ['ogbl-citation2', 'ogbl-ppa']:
                print("S3GRLHeavy selected")
                model = S3GRLHeavy(args.hidden_channels, sign_k, train_dataset,
                                   args.use_feature, node_embedding=emb, pool_operatorwise=args.pool_operatorwise,
                                   dropout=args.dropout, k_heuristic=args.k_heuristic,
                                   k_pool_strategy=args.k_pool_strategy, use_mlp=args.use_mlp).to(device)

            else:
                print("S3GRLLight selected")
                model = S3GRLLight(args.hidden_channels, sign_k, train_dataset,
                                   args.use_feature, node_embedding=emb, pool_operatorwise=args.pool_operatorwise,
                                   dropout=args.dropout, k_heuristic=args.k_heuristic,
                                   k_pool_strategy=args.k_pool_strategy, use_mlp=args.use_mlp).to(device)
        print(f"Model architecture is: {model}")
        parameters = list(model.parameters())
        if args.train_node_embedding:
            torch.nn.init.xavier_uniform_(emb.weight)
            parameters += list(emb.parameters())
        optimizer = torch.optim.Adam(params=parameters, lr=args.lr, weight_decay=1e-4)
        scd = lr_scheduler.ReduceLROnPlateau(optimizer, patience=10,
                                             min_lr=5e-5)
        total_params = sum(p.numel() for param in parameters for p in param)
        if init_features == "n2v":
            total_params += n2v_params
        print(f'Total number of parameters is {total_params}')

        if args.model == 'DGCNN':
            print(f'SortPooling k is set to {model.k}')
        with open(log_file, 'a') as f:
            print(f'Total number of parameters is {total_params}', file=f)
            if args.model == 'DGCNN':
                print(f'SortPooling k is set to {model.k}', file=f)

        start_epoch = 1
        if args.continue_from is not None or args.only_test or args.test_multiple_models:
            raise NotImplementedError("args continue_from/only_test/test_multiple_models are legacy and not supported.")

        # Training starts
        all_stats = []
        all_inference_times = []
        all_train_times = []
        for epoch in range(start_epoch, start_epoch + args.epochs):
            if args.profile:
                # this gives the stats for exactly one training epoch
                if epoch == 1 and args.dynamic_train and args.cache_dynamic:
                    train_loader.num_workers = 0
                if epoch == 1 and args.dynamic_val and args.cache_dynamic:
                    val_loader.num_workers = 0
                if epoch == 1 and args.dynamic_test and args.cache_dynamic:
                    test_loader.num_workers = 0
                loss, stats = profile_train(model, train_loader, optimizer, device, emb, train_dataset, args)
                all_stats.append(stats)
            else:
                if epoch == 1 and args.dynamic_train and args.cache_dynamic:
                    train_loader.num_workers = 0
                if epoch == 1 and args.dynamic_val and args.cache_dynamic:
                    val_loader.num_workers = 0
                if epoch == 1 and args.dynamic_test and args.cache_dynamic:
                    test_loader.num_workers = 0

                if not args.pairwise:
                    time_start_for_train_epoch = default_timer()
                    loss = train_bce(model, train_loader, optimizer, device, emb, train_dataset, args, epoch)
                    time_end_for_train_epoch = default_timer()
                    scd.step(loss)
                    all_train_times.append(time_end_for_train_epoch - time_start_for_train_epoch)
                else:
                    loss = train_pairwise(model, train_pos_loader, train_neg_loader, optimizer, device, emb,
                                          train_dataset,
                                          args, epoch)

            if epoch % args.eval_steps == 0:
                results, time_for_inference = test(evaluator, model, val_loader, device, emb, test_loader, args)
                all_inference_times.append(time_for_inference)
                if hypertuning:
                    tune.report(val_loss=loss, val_accuracy=results['AUC'][0])
                for key, result in results.items():
                    loggers[key].add_result(run, result)

                if epoch % args.log_steps == 0:
                    if args.checkpoint_training:
                        model_name = os.path.join(
                            args.res_dir, 'run{}_model_checkpoint{}.pth'.format(run + 1, epoch))
                        optimizer_name = os.path.join(
                            args.res_dir, 'run{}_optimizer_checkpoint{}.pth'.format(run + 1, epoch))
                        torch.save(model.state_dict(), model_name)
                        torch.save(optimizer.state_dict(), optimizer_name)

                    for key, result in results.items():
                        valid_res, test_res = result
                        to_print = (f'Run: {run + 1:02d}, Epoch: {epoch:02d}, ' +
                                    f'Loss: {loss:.4f}, Valid: {100 * valid_res:.2f}%, ' +
                                    f'Test: {100 * test_res:.2f}%')
                        print(key)
                        print(to_print)
                        with open(log_file, 'a') as f:
                            print(key, file=f)
                            print(to_print, file=f)
                with open(log_file, 'a') as f:
                    for key, result in results.items():
                        print(key)
                        picked_val, picked_test = loggers[key].print_best_picked(run, f=f)
                        print(f'Picked Valid: {picked_val:.2f}, Picked Test: {picked_test:.2f}')
            else:
                print(f"Eval on validation and test is skipped. Completed epochs: {epoch}.")
            if epoch == 1 and args.dynamic_train and args.cache_dynamic:
                train_loader.dataset.set_use_cache(True, id="train")
                train_loader.num_workers = args.num_workers

            if epoch == 1 and args.dynamic_val and args.cache_dynamic:
                val_loader.dataset.set_use_cache(True, id="val")
                val_loader.dataset.num_workers = args.num_workers

            if epoch == 1 and args.dynamic_test and args.cache_dynamic:
                test_loader.dataset.set_use_cache(True, id="test")
                test_loader.dataset.num_workers = args.num_workers

        if args.profile:
            extra_identifier = ''
            if args.model == "SIGN":
                extra_identifier = f"{args.k_heuristic}{args.sign_type}{args.hidden_channels}"
            stats_suffix = f'{args.model}_{args.dataset}{args.data_appendix}_seed_{args.seed}_id_{extra_identifier}'
            profile_helper(all_stats, model, train_dataset, stats_suffix, all_inference_times, total_prep_time)

        for key in loggers.keys():
            print(key)
            loggers[key].add_info(args.epochs, args.runs)
            loggers[key].print_statistics(run)
            with open(log_file, 'a') as f:
                print(key, file=f)
                loggers[key].print_statistics(run, f=f)

    best_test_scores = []
    for key in loggers.keys():
        print(key)
        loggers[key].add_info(args.epochs, args.runs)
        best_test_scores += [loggers[key].print_statistics()]
        with open(log_file, 'a') as f:
            print(key, file=f)
            loggers[key].print_statistics(f=f)
    print(f'Total number of parameters is {total_params}')
    print(f'Results are saved in {args.res_dir}')

    if args.delete_dataset:
        if os.path.exists(path):
            shutil.rmtree(path)

    if True:
        # Delete results each time.
        if os.path.exists(args.res_dir):
            shutil.rmtree(args.res_dir)

    print("fin.")

    if args.dataset == 'ogbl-collab':
        best = best_test_scores[1]  # hits@50
    elif args.dataset == 'ogbl-ddi':
        best = best_test_scores[0]  # hits@20
    elif args.dataset == 'ogbl-ppa':
        best = best_test_scores[2]  # hits@100
    elif args.dataset == 'ogbl-citation2':
        best = best_test_scores[0]  # MRR
    elif args.dataset == 'ogbl-vessel':
        best = best_test_scores[0]  # aucroc
    elif args.dataset.lower() in ['cora', 'citeseer', 'pubmed']:
        best = best_test_scores[2]  # hits@100
    else:
        best = best_test_scores[0]  # auc
    return total_prep_time, best, all_train_times, all_inference_times, total_params


@timeit()
def run_sgrl_with_run_profiling(args, device):
    total_prep_time, best_test_scores, all_train_times, all_inference_times, total_params = run_sgrl_learning(args,
                                                                                                              device)
    return total_prep_time, best_test_scores, all_train_times, all_inference_times, total_params


if __name__ == '__main__':
    # Data settings
    parser = argparse.ArgumentParser(description='SGRL model run helper')
    parser.add_argument('--dataset', type=str, default='ogbl-collab')
    parser.add_argument('--fast_split', action='store_true',
                        help="for large custom datasets (not OGB), do a fast data split")
    # GNN settings
    parser.add_argument('--model', type=str, default='DGCNN')
    parser.add_argument('--sortpool_k', type=float, default=0.6)
    parser.add_argument('--num_layers', type=int, default=3)
    parser.add_argument('--hidden_channels', type=int, default=32)
    parser.add_argument('--batch_size', type=int, default=32)
    # Subgraph extraction settings
    parser.add_argument('--num_hops', type=int, default=1)
    parser.add_argument('--ratio_per_hop', type=float, default=1.0)
    parser.add_argument('--max_nodes_per_hop', type=int, default=None)
    parser.add_argument('--node_label', type=str, default='drnl',
                        help="which specific labeling trick to use")
    parser.add_argument('--use_feature', action='store_true',
                        help="whether to use raw node features as GNN input")
    parser.add_argument('--use_edge_weight', action='store_true',
                        help="whether to consider edge weight in GNN")
    # Training settings
    parser.add_argument('--lr', type=float, default=0.0001)
    parser.add_argument('--epochs', type=int, default=50)
    parser.add_argument('--runs', type=int, default=1)
    parser.add_argument('--train_percent', type=float, default=100)
    parser.add_argument('--val_percent', type=float, default=100)
    parser.add_argument('--test_percent', type=float, default=100)
    parser.add_argument('--dynamic_train', action='store_true',
                        help="dynamically extract enclosing subgraphs on the fly")
    parser.add_argument('--dynamic_val', action='store_true')
    parser.add_argument('--dynamic_test', action='store_true')
    parser.add_argument('--num_workers', type=int, default=16,
                        help="number of workers for dynamic mode; 0 if not dynamic")
    parser.add_argument('--train_node_embedding', action='store_true',
                        help="also train free-parameter node embeddings together with GNN")
    parser.add_argument('--pretrained_node_embedding', type=str, default=None,
                        help="load pretrained node embeddings as additional node features")
    # Testing settings
    parser.add_argument('--use_valedges_as_input', action='store_true')
    parser.add_argument('--eval_steps', type=int, default=1)
    parser.add_argument('--log_steps', type=int, default=1)
    parser.add_argument('--checkpoint_training', action='store_true')
    parser.add_argument('--data_appendix', type=str, default='',
                        help="an appendix to the data directory")
    parser.add_argument('--save_appendix', type=str, default='',
                        help="an appendix to the save directory")
    parser.add_argument('--keep_old', action='store_true',
                        help="do not overwrite old files in the save directory")
    parser.add_argument('--delete_dataset', action='store_true',
                        help="delete existing datasets folder before running new command")
    parser.add_argument('--continue_from', type=int, default=None,
                        help="from which epoch's checkpoint to continue training")
    parser.add_argument('--only_test', action='store_true',
                        help="only test without training")
    parser.add_argument('--test_multiple_models', action='store_true',
                        help="test multiple models together")
    parser.add_argument('--use_heuristic', type=str, default=None,
                        help="test a link prediction heuristic (CN or AA)")
    parser.add_argument('--dataset_stats', action='store_true',
                        help="Print dataset statistics")
    parser.add_argument('--m', type=int, default=0, help="Set rw length")
    parser.add_argument('--M', type=int, default=0, help="Set number of rw")
    parser.add_argument('--dropedge', type=float, default=.0, help="Drop Edge Value for initial edge_index")
    parser.add_argument('--cuda_device', type=int, default=0, help="Only set available the passed GPU")

    parser.add_argument('--calc_ratio', action='store_true', help="Calculate overall sparsity ratio")
    parser.add_argument('--pairwise', action='store_true',
                        help="Choose to override the BCE loss to pairwise loss functions")
    parser.add_argument('--loss_fn', type=str, help="Choose the loss function")
    parser.add_argument('--neg_ratio', type=int, default=1,
                        help="Compile neg_ratio times the positive samples for compiling neg_samples"
                             "(only for Training data)")
    parser.add_argument('--profile', action='store_true', help="Run the PyG profiler for each epoch")
    parser.add_argument('--split_val_ratio', type=float, default=0.05)
    parser.add_argument('--split_test_ratio', type=float, default=0.1)
    parser.add_argument('--train_mlp', action='store_true',
                        help="Train using structure unaware mlp")
    parser.add_argument('--train_gae', action='store_true', help="Train GAE on the dataset")
    parser.add_argument('--base_gae', type=str, default='', help='Choose base GAE model', choices=['GCN', 'SAGE'])

    parser.add_argument('--dropout', type=float, default=0.5)
    parser.add_argument('--seed', type=int, default=1)  # we can set this to value in dataset_split_num as well
    parser.add_argument('--dataset_split_num', type=int, default=1)  # This is maintained for WalkPool Datasets only

    parser.add_argument('--train_n2v', action='store_true', help="Train node2vec on the dataset")
    parser.add_argument('--train_mf', action='store_true', help="Train MF on the dataset")

    parser.add_argument('--sign_k', type=int, default=3)
    parser.add_argument('--sign_type', type=str, default='', required=False, choices=['PoS', 'SoP', 'hybrid'])

    # Do note that pool_operatorwise is outdated and not used. This is passed to the model, but, never consumed.
    parser.add_argument('--pool_operatorwise', action='store_true', default=False, required=False)

    parser.add_argument('--optimize_sign', action='store_true', default=False, required=False)
    parser.add_argument('--init_features', type=str, default='',
                        help='Choose to augment node features with either one-hot encoding or their degree values',
                        choices=['degree', 'eye', 'n2v'])
    parser.add_argument('--n2v_dim', type=int, default=256)

    parser.add_argument('--k_heuristic', type=int, default=0)
    parser.add_argument('--k_node_set_strategy', type=str, default="", required=False,
                        choices=['union', 'intersection'])
    parser.add_argument('--k_pool_strategy', type=str, default="", required=False, choices=['mean', 'concat', 'sum'])
    parser.add_argument('--init_representation', type=str, choices=['GIC', 'ARGVA', 'GAE', 'VGAE'])

    parser.add_argument('--cache_dynamic', action='store_true', default=False, required=False)
    parser.add_argument('--split_by_year', action='store_true', default=False, required=False)
    parser.add_argument('--use_mlp', action='store_true', default=False, required=False)
    parser.add_argument('--normalize_feats', action='store_true', default=False, required=False)
    parser.add_argument('--size_only', action='store_true', default=False, required=False)

    args = parser.parse_args()

    device = torch.device(f'cuda:{args.cuda_device}' if torch.cuda.is_available() else 'cpu')
    print(f"Using device: {device}")

    seed_everything(args.seed)

    if args.model == "SIGN" and not args.init_features and not args.use_feature:
        raise Exception("Need to init features to have SIGN work. (X) cannot be None. Choose bet. I, Deg and n2v.")

    if args.profile and not torch.cuda.is_available():
        raise Exception("CUDA needs to be enabled to run PyG profiler")

    if args.profile:
        run_sgrl_with_run_profiling(args, device)
    else:
        start = default_timer()
        total_prep_time, _, _, _, _ = run_sgrl_learning(args, device)
        end = default_timer()
        print(f"Time taken for dataset prep: {total_prep_time:.2f} seconds")
        print(f"Time taken for run: {end - start:.2f} seconds")
