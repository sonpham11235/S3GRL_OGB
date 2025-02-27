import math
import numpy as np
import torch
from torch.nn import (ModuleList, Linear, Conv1d, MaxPool1d, Embedding, ReLU,
                      Sequential, BatchNorm1d as BN, BatchNorm1d)
import torch.nn.functional as F
from torch_geometric.nn import GCNConv, SAGEConv, GINConv, global_sort_pool, global_add_pool, global_mean_pool, MLP, \
    global_max_pool
from torch_geometric.utils import dropout_adj


class GCN(torch.nn.Module):
    def __init__(self, hidden_channels, num_layers, max_z, train_dataset,
                 use_feature=False, node_embedding=None, dropout=0.5, dropedge=0.0):
        super(GCN, self).__init__()

        self.use_feature = use_feature
        self.node_embedding = node_embedding
        self.max_z = max_z
        self.z_embedding = Embedding(self.max_z, hidden_channels)

        self.convs = ModuleList()
        initial_channels = hidden_channels
        if self.use_feature:
            initial_channels += train_dataset.num_features
        if self.node_embedding is not None:
            initial_channels += node_embedding.embedding_dim
        self.convs.append(GCNConv(initial_channels, hidden_channels))
        for _ in range(num_layers - 1):
            self.convs.append(GCNConv(hidden_channels, hidden_channels))

        self.dropout = dropout
        self.dropedge = dropedge
        self.mlp = MLP([hidden_channels, hidden_channels, 1], dropout=dropout, batch_norm=True)

    def reset_parameters(self):
        for conv in self.convs:
            conv.reset_parameters()

    def forward(self, num_nodes, z, edge_index, batch, x=None, edge_weight=None, node_id=None):
        edge_index, _ = dropout_adj(edge_index, p=self.dropedge,
                                    force_undirected=True,
                                    num_nodes=num_nodes,
                                    training=self.training)

        z_emb = self.z_embedding(z)
        if z_emb.ndim == 3:  # in case z has multiple integer labels
            z_emb = z_emb.sum(dim=1)
        if self.use_feature and x is not None:
            x = torch.cat([z_emb, x.to(torch.float)], 1)
        else:
            x = z_emb
        if self.node_embedding is not None and node_id is not None:
            n_emb = self.node_embedding(node_id)
            x = torch.cat([x, n_emb], 1)
        for conv in self.convs[:-1]:
            x = conv(x, edge_index, edge_weight)
            x = F.relu(x)
            x = F.dropout(x, p=self.dropout, training=self.training)
        x = self.convs[-1](x, edge_index, edge_weight)

        # center pooling
        _, center_indices = np.unique(batch.cpu().numpy(), return_index=True)
        x_src = x[center_indices]
        x_dst = x[center_indices + 1]
        x = (x_src * x_dst)

        # sum pool
        # x = global_add_pool(x, batch)

        # max pool
        # x = global_max_pool(x, batch)

        x = self.mlp(x)
        return x


class SAGE(torch.nn.Module):
    def __init__(self, hidden_channels, num_layers, max_z, train_dataset=None,
                 use_feature=False, node_embedding=None, dropout=0.5, dropedge=0.0):
        super(SAGE, self).__init__()
        self.use_feature = use_feature
        self.node_embedding = node_embedding
        self.max_z = max_z
        self.z_embedding = Embedding(self.max_z, hidden_channels)

        self.convs = ModuleList()
        initial_channels = hidden_channels
        if self.use_feature:
            initial_channels += train_dataset.num_features
        if self.node_embedding is not None:
            initial_channels += node_embedding.embedding_dim
        self.convs.append(SAGEConv(initial_channels, hidden_channels))
        for _ in range(num_layers - 1):
            self.convs.append(SAGEConv(hidden_channels, hidden_channels))

        self.dropout = dropout
        self.dropedge = dropedge
        self.mlp = MLP([hidden_channels, hidden_channels, 1], dropout=dropout, batch_norm=True)

    def reset_parameters(self):
        for conv in self.convs:
            conv.reset_parameters()

    def forward(self, num_nodes, z, edge_index, batch, x=None, edge_weight=None, node_id=None):
        edge_index, _ = dropout_adj(edge_index, p=self.dropedge,
                                    force_undirected=True,
                                    num_nodes=num_nodes,
                                    training=self.training)
        z_emb = self.z_embedding(z)
        if z_emb.ndim == 3:  # in case z has multiple integer labels
            z_emb = z_emb.sum(dim=1)
        if self.use_feature and x is not None:
            x = torch.cat([z_emb, x.to(torch.float)], 1)
        else:
            x = z_emb
        if self.node_embedding is not None and node_id is not None:
            n_emb = self.node_embedding(node_id)
            x = torch.cat([x, n_emb], 1)
        for conv in self.convs[:-1]:
            x = conv(x, edge_index)
            x = F.relu(x)
            x = F.dropout(x, p=self.dropout, training=self.training)
        x = self.convs[-1](x, edge_index)
        if True:  # center pooling
            _, center_indices = np.unique(batch.cpu().numpy(), return_index=True)
            x_src = x[center_indices]
            x_dst = x[center_indices + 1]
            x = (x_src * x_dst)
            x = self.mlp(x)
        else:  # max pooling
            x = global_max_pool(x, batch)
            x = self.mlp(x)

        return x


# An end-to-end deep learning architecture for graph classification, AAAI-18.
class DGCNN(torch.nn.Module):
    def __init__(self, hidden_channels, num_layers, max_z, k=0.6, train_dataset=None,
                 dynamic_train=False, GNN=GCNConv, use_feature=False,
                 node_embedding=None, dropedge=0.0):
        super(DGCNN, self).__init__()

        self.use_feature = use_feature
        self.node_embedding = node_embedding

        if k <= 1:  # Transform percentile to number.
            if train_dataset is None:
                k = 30
            else:
                if dynamic_train:
                    sampled_train = train_dataset[:1000]
                else:
                    sampled_train = train_dataset
                num_nodes = sorted([g.num_nodes for g in sampled_train])
                k = num_nodes[int(math.ceil(k * len(num_nodes))) - 1]
                k = max(10, k)
        self.k = int(k)

        self.max_z = max_z
        self.z_embedding = Embedding(self.max_z, hidden_channels)

        self.convs = ModuleList()
        initial_channels = hidden_channels
        if self.use_feature:
            initial_channels += train_dataset.num_features
        if self.node_embedding is not None:
            initial_channels += node_embedding.embedding_dim

        self.convs.append(GNN(initial_channels, hidden_channels))
        for i in range(0, num_layers - 1):
            self.convs.append(GNN(hidden_channels, hidden_channels))
        self.convs.append(GNN(hidden_channels, 1))

        conv1d_channels = [16, 32]
        total_latent_dim = hidden_channels * num_layers + 1
        conv1d_kws = [total_latent_dim, 5]
        self.conv1 = Conv1d(1, conv1d_channels[0], conv1d_kws[0],
                            conv1d_kws[0])
        self.maxpool1d = MaxPool1d(2, 2)
        self.conv2 = Conv1d(conv1d_channels[0], conv1d_channels[1],
                            conv1d_kws[1], 1)
        dense_dim = int((self.k - 2) / 2 + 1)

        dense_dim = (dense_dim - conv1d_kws[1] + 1) * conv1d_channels[1]
        self.dropedge = dropedge
        self.mlp = MLP([dense_dim, 128, 1], dropout=0.5, batch_norm=True)

    def forward(self, num_nodes, z, edge_index, batch, x=None, edge_weight=None, node_id=None):
        edge_index, _ = dropout_adj(edge_index, p=self.dropedge,
                                    force_undirected=True,
                                    num_nodes=num_nodes,
                                    training=self.training)

        z_emb = self.z_embedding(z)
        if z_emb.ndim == 3:  # in case z has multiple integer labels
            z_emb = z_emb.sum(dim=1)
        if self.use_feature and x is not None:
            x = torch.cat([z_emb, x.to(torch.float)], 1)
        else:
            x = z_emb
        if self.node_embedding is not None and node_id is not None:
            n_emb = self.node_embedding(node_id)
            x = torch.cat([x, n_emb], 1)
        xs = [x]

        for conv in self.convs:
            xs += [torch.tanh(conv(xs[-1], edge_index, edge_weight))]
        x = torch.cat(xs[1:], dim=-1)

        # Global pooling.
        x = global_sort_pool(x, batch, self.k)
        x = x.unsqueeze(1)  # [num_graphs, 1, k * hidden]
        x = F.relu(self.conv1(x))
        x = self.maxpool1d(x)
        x = F.relu(self.conv2(x))
        x = x.view(x.size(0), -1)  # [num_graphs, dense_dim]

        # MLP.
        x = self.mlp(x)
        return x


class GIN(torch.nn.Module):
    def __init__(self, hidden_channels, num_layers, max_z, train_dataset,
                 use_feature=False, node_embedding=None, dropout=0.5,
                 jk=True, train_eps=False, dropedge=0.0):
        super(GIN, self).__init__()
        self.use_feature = use_feature
        self.node_embedding = node_embedding
        self.max_z = max_z
        self.z_embedding = Embedding(self.max_z, hidden_channels)
        self.jk = jk

        initial_channels = hidden_channels
        if self.use_feature:
            initial_channels += train_dataset.num_features
        if self.node_embedding is not None:
            initial_channels += node_embedding.embedding_dim
        self.conv1 = GINConv(
            Sequential(
                Linear(initial_channels, hidden_channels),
                ReLU(),
                Linear(hidden_channels, hidden_channels),
                ReLU(),
                BN(hidden_channels),
            ),
            train_eps=train_eps)
        self.convs = torch.nn.ModuleList()
        for i in range(num_layers - 1):
            self.convs.append(
                GINConv(
                    Sequential(
                        Linear(hidden_channels, hidden_channels),
                        ReLU(),
                        Linear(hidden_channels, hidden_channels),
                        ReLU(),
                        BN(hidden_channels),
                    ),
                    train_eps=train_eps))

        self.dropout = dropout
        if self.jk:
            self.mlp = MLP([num_layers * hidden_channels, hidden_channels, 1], dropout=0.5, batch_norm=True)
        else:
            self.mlp = MLP([hidden_channels, hidden_channels, 1], dropout=0.5, batch_norm=True)

        self.dropedge = dropedge

    def forward(self, num_nodes, z, edge_index, batch, x=None, edge_weight=None, node_id=None):
        edge_index, _ = dropout_adj(edge_index, p=self.dropedge,
                                    force_undirected=True,
                                    num_nodes=num_nodes,
                                    training=self.training)

        z_emb = self.z_embedding(z)
        if z_emb.ndim == 3:  # in case z has multiple integer labels
            z_emb = z_emb.sum(dim=1)
        if self.use_feature and x is not None:
            x = torch.cat([z_emb, x.to(torch.float)], 1)
        else:
            x = z_emb
        if self.node_embedding is not None and node_id is not None:
            n_emb = self.node_embedding(node_id)
            x = torch.cat([x, n_emb], 1)
        x = self.conv1(x, edge_index)
        xs = [x]
        for conv in self.convs:
            x = conv(x, edge_index)
            xs += [x]
        if self.jk:
            x = global_mean_pool(torch.cat(xs, dim=1), batch)
        else:
            x = global_mean_pool(xs[-1], batch)
        x = self.mlp(x)

        return x


class S3GRLLight(torch.nn.Module):
    def __init__(self, hidden_channels, num_layers, train_dataset, use_feature=False, node_embedding=None, dropout=0.5,
                 pool_operatorwise=False, k_heuristic=0, k_pool_strategy="", use_mlp=False):
        super().__init__()

        self.use_feature = use_feature
        self.node_embedding = node_embedding

        self.dropout = dropout
        self.pool_operatorwise = pool_operatorwise  # pool at the operator level, esp. useful for SoP
        self.k_heuristic = k_heuristic  # k-heuristic in k-heuristic PoS Plus
        self.k_pool_strategy = k_pool_strategy  # k-heuristic pool strat
        self.hidden_channels = hidden_channels
        initial_channels = hidden_channels

        initial_channels += train_dataset.num_features - hidden_channels
        if self.node_embedding is not None:
            initial_channels += node_embedding.embedding_dim

        if not use_mlp:
            # note; operator_diff MLP is just a linear layer that corresponds to a weight matrix, W
            mlp_layers = [initial_channels * (num_layers + 1), hidden_channels]
            self.operator_diff = MLP(mlp_layers, dropout=dropout, batch_norm=True, act_first=True, act='relu',
                                     plain_last=False)
        else:
            mlp_layers = [initial_channels * (num_layers + 1), hidden_channels, hidden_channels]
            self.operator_diff = MLP(mlp_layers, dropout=dropout, batch_norm=True, act_first=True, act='relu',
                                     plain_last=True)
        if not self.k_heuristic:
            self.link_pred_mlp = MLP([hidden_channels, hidden_channels, 1], dropout=dropout, batch_norm=True,
                                     act_first=True, act='relu')
        else:
            if self.k_pool_strategy == "mean":
                channels = 2
            elif self.k_pool_strategy == "sum":
                channels = 2
            elif self.k_pool_strategy == "max":
                channels = 2
            elif self.k_pool_strategy == "concat":
                channels = 1 + self.k_heuristic
            else:
                raise NotImplementedError(f"Check pool strat: {self.k_pool_strategy}")
            self.link_pred_mlp = MLP([hidden_channels * channels, hidden_channels, 1], dropout=dropout,
                                     batch_norm=True, act_first=True, act='relu')
        self._uniform_norm()

    def _uniform_norm(self):
        for lin in self.link_pred_mlp.lins:
            torch.nn.init.xavier_uniform_(lin.weight.data)
            lin.bias.data.fill_(0.0)

        for lin in self.operator_diff.lins:
            torch.nn.init.xavier_uniform_(lin.weight.data)
            lin.bias.data.fill_(0.0)

    def _centre_pool_helper(self, batch, h, op_index):
        # center pooling
        uq, center_indices = np.unique(batch[op_index].cpu().numpy(), return_index=True)
        if not self.k_heuristic:
            # batch_size X hidden_dim
            h_src = h[center_indices]
            h_dst = h[center_indices + 1]
            h = (h_src * h_dst)
        else:
            h_src = h[center_indices]
            h_dst = h[center_indices + 1]
            h_a = h_src * h_dst

            mask = torch.ones(size=(batch[op_index].size()), dtype=torch.bool)
            mask[center_indices] = False
            mask[center_indices + 1] = False
            trimmed_batch = batch[op_index][mask]

            if self.k_pool_strategy == 'max':
                h_k_max = global_max_pool(h[mask], trimmed_batch, size=uq.shape[0])
                h = torch.concat([h_a, h_k_max], dim=-1)
            elif self.k_pool_strategy == 'mean':
                h_k_mean = global_mean_pool(h[mask], trimmed_batch, size=uq.shape[0])
                h = torch.concat([h_a, h_k_mean], dim=-1)
            elif self.k_pool_strategy == 'sum':
                h_k_sum = global_add_pool(h[mask], trimmed_batch, size=uq.shape[0])
                h = torch.concat([h_a, h_k_sum], dim=-1)
            elif self.k_pool_strategy == 'concat':
                raise NotImplementedError("Concat pool strat is not supported.")
                h_k = h[mask].reshape(shape=(
                    center_indices.shape[0], self.hidden_channels * self.k_heuristic)
                )
                h = torch.concat([h_a, h_k], dim=-1)

        return h

    def forward(self, xs, batch):
        xs_cat = torch.cat(xs, dim=-1)
        x = xs_cat
        x = self.operator_diff(x)

        x = self._centre_pool_helper(batch, x, -1)

        x = self.link_pred_mlp(x)
        return x

    def reset_parameters(self):
        self._uniform_norm()
        self.operator_diff.reset_parameters()
        self.link_pred_mlp.reset_parameters()


class S3GRLHeavy(torch.nn.Module):
    def __init__(self, hidden_channels, num_layers, train_dataset, use_feature=False, node_embedding=None, dropout=0.5,
                 pool_operatorwise=False, k_heuristic=0, k_pool_strategy="", use_mlp=True):
        # Extra wide and deep version of S3GRL. only used for citation2 for now.

        super().__init__()
        self.use_feature = use_feature
        self.node_embedding = node_embedding

        self.dropout = dropout
        self.pool_operatorwise = pool_operatorwise  # pool at the operator level, esp. useful for SoP
        self.k_heuristic = k_heuristic  # k-heuristic in k-heuristic PoS Plus
        self.k_pool_strategy = k_pool_strategy  # k-heuristic pool strat
        self.hidden_channels = hidden_channels
        initial_channels = hidden_channels

        initial_channels += train_dataset.num_features - hidden_channels
        if self.node_embedding is not None:
            initial_channels += node_embedding.embedding_dim

        if not use_mlp:
            # note; operator_diff MLP is just a linear layer that corresponds to a weight matrix, W
            mlp_layers = [initial_channels * (num_layers + 1), hidden_channels]
            self.operator_diff = MLP(mlp_layers, dropout=dropout, batch_norm=True, act_first=True, act='relu',
                                     plain_last=False)
        else:
            mlp_layers = [initial_channels * (num_layers + 1), hidden_channels, hidden_channels]
            self.operator_diff = MLP(mlp_layers, dropout=dropout, batch_norm=True, act_first=True, act='relu',
                                     plain_last=True)

        if not self.k_heuristic:
            self.link_pred_mlp = MLP(
                [hidden_channels, hidden_channels * 2, hidden_channels, hidden_channels, 1],
                dropout=dropout, batch_norm=True, act_first=True, act='elu')
        else:
            if self.k_pool_strategy == "mean":
                channels = 2
            elif self.k_pool_strategy == "sum":
                channels = 2
            elif self.k_pool_strategy == "max":
                channels = 2
            elif self.k_pool_strategy == "concat":
                channels = 1 + self.k_heuristic
            else:
                raise NotImplementedError(f"Check pool strat: {self.k_pool_strategy}")
            self.link_pred_mlp = MLP(
                [hidden_channels * channels, hidden_channels * 2, hidden_channels, hidden_channels, 1],
                dropout=dropout,
                batch_norm=True,
                act_first=True, act='elu')
        self._uniform_norm()

    def _uniform_norm(self):
        for lin in self.link_pred_mlp.lins:
            torch.nn.init.xavier_uniform_(lin.weight.data)
            lin.bias.data.fill_(0.0)

        for lin in self.operator_diff.lins:
            torch.nn.init.xavier_uniform_(lin.weight.data)
            lin.bias.data.fill_(0.0)

    def forward(self, xs, batch):
        x = torch.cat(xs, dim=-1)
        x = self.operator_diff(x)

        x = self._centre_pool_helper(batch, x, -1)

        x = self.link_pred_mlp(x)
        return x

    def reset_parameters(self):
        self._uniform_norm()
        self.operator_diff.reset_parameters()
        self.link_pred_mlp.reset_parameters()

    def _centre_pool_helper(self, batch, h, op_index):
        # center pooling
        uq, center_indices = np.unique(batch[op_index].cpu().numpy(), return_index=True)
        if not self.k_heuristic:
            # batch_size X hidden_dim
            h_src = h[center_indices]
            h_dst = h[center_indices + 1]
            h = (h_src * h_dst)
        else:
            h_src = h[center_indices]
            h_dst = h[center_indices + 1]
            h_a = h_src * h_dst

            mask = torch.ones(size=(batch[op_index].size()), dtype=torch.bool)
            mask[center_indices] = False
            mask[center_indices + 1] = False
            trimmed_batch = batch[op_index][mask]

            if self.k_pool_strategy == 'max':
                h_k_max = global_max_pool(h[mask], trimmed_batch, size=uq.shape[0])
                h = torch.concat([h_a, h_k_max], dim=-1)
            elif self.k_pool_strategy == 'mean':
                h_k_mean = global_mean_pool(h[mask], trimmed_batch, size=uq.shape[0])
                h = torch.concat([h_a, h_k_mean], dim=-1)
            elif self.k_pool_strategy == 'sum':
                h_k_sum = global_add_pool(h[mask], trimmed_batch, size=uq.shape[0])
                h = torch.concat([h_a, h_k_sum], dim=-1)
            elif self.k_pool_strategy == 'concat':
                raise NotImplementedError("Concat pool strat is not supported.")
                h_k = h[mask].reshape(shape=(
                    center_indices.shape[0], self.hidden_channels * self.k_heuristic)
                )
                h = torch.concat([h_a, h_k], dim=-1)

        return h
