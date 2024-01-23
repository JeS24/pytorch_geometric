import argparse
import os
import time
from typing import Dict, List, Tuple

import torch
import torch.distributed as dist
import torch.multiprocessing as mp
import torch.nn.functional as F
from ogb.lsc import MAG240MDataset, MAG240MEvaluator
from torch import Tensor
from torch.nn import Embedding, Linear
from torch.nn.parallel import DistributedDataParallel
from torchmetrics import Accuracy
from tqdm import tqdm

from torch_geometric import seed_everything
from torch_geometric.data import Batch
from torch_geometric.loader.neighbor_loader import NeighborLoader
from torch_geometric.nn import HeteroConv, SAGEConv
from torch_geometric.nn.norm import BatchNorm
from torch_geometric.typing import Adj, EdgeType, NodeType


def common_step(batch: Batch, model) -> Tuple[Tensor, Tensor]:
    batch_size = batch["paper"].batch_size
    y_hat = model(batch)["paper"][:batch_size]
    y = batch["paper"].y[:batch_size].to(torch.long)
    return y_hat, y


def training_step(batch: Batch, acc, model) -> Tensor:
    y_hat, y = common_step(batch, model)
    train_loss = F.cross_entropy(y_hat, y)
    train_acc = acc(y_hat.softmax(dim=-1), y)
    return train_loss, train_acc


def validation_step(batch: Batch, acc, model):
    y_hat, y = common_step(batch, model)
    return acc(y_hat.softmax(dim=-1), y)


def predict_step(batch: Batch):
    y_hat, y = common_step(batch, model)
    return y_hat


class SAGEConvLayer(torch.nn.Module):
    def __init__(
        self,
        in_feat,
        out_feat,
        dropout,
        metadata,
    ):
        super().__init__()
        self.in_feat = in_feat
        self.out_feat = out_feat
        self.conv = HeteroConv({
            e_type: conv_type(in_feat, out_feat, **kwargs)
            for e_type in metadata[1]
        })
        self.dropout_conv = nn.Dropout(dropout)
        self.activation = torch.nn.ReLU()
        self.normalizations = nn.ModuleDict()
        for node in metadata[0]:
            self.normalizations[node] = BatchNorm(out_feat)

    def forward(self, x_dict, edge_index_dict):
        h = self.conv(x_dict, edge_index_dict)
        for node_type in h.keys():
            h[node_type] = self.normalizations[node_type](self.activation(
                self.dropout_conv(h[node_type])))
        return h


class GraphSAGE(torch.nn.Module):
    def __init__(
        self,
        in_channels,
        hidden_channels,
        num_layers,
        out_channels,
        dropout,
        data,
    ):
        super().__init__()
        self.num_layers = num_layers
        self.author_embed = torch.nn.Embedding(data['author'].num_nodes,
                                               in_channels)
        self.institution_embed = torch.nn.Embedding(
            data['institution'].num_nodes, in_channels)
        self.metadata = data.metadata()
        self.input_conv = SAGEConvLayer(in_channels, hidden_channels, dropout,
                                        self.metadata)
        self.hidden_convs = []
        if self.num_layers > 2:
            for i in range(num_layers - 2):
                self.hidden_convs.append(
                    SAGEConvLayer(hidden_channels, hidden_channels, dropout,
                                  self.metadata))
        self.output_conv = SAGEConvLayer(hidden_channels, out_channels,
                                         dropout, self.metadata)

    def forward(self, batch):
        x_dict = {'paper': batch['paper'].x}
        x_dict['author'] = self.author_embed(batch['author'].n_id)
        x_dict['institution'] = self.institution_embed(
            batch['institution'].n_id)
        edge_index_dict = batch.collect('edge_index')
        x_dict = self.input_conv(x_dict, edge_index_dict)
        if self.num_layers > 2:
            for i in range(num_layers - 2):
                x_dict = self.hidden_convs[i](x_dict, edge_index_dict)
        x_dict = self.output_conv(x_dict, edge_index_dict)
        return x_dict


def run(
    rank,
    data,
    n_devices=1,
    num_epochs=1,
    num_steps_per_epoch=-1,
    log_every_n_steps=1,
    batch_size=1024,
    sizes=[128],
    hidden_channels=1024,
    dropout=0.5,
    eval_steps=100,
    num_warmup_iters_for_timing=10,
    debug=False,
):
    seed_everything(12345)
    if n_devices > 1:
        if rank == 0:
            print("Setting up distributed...")
        os.environ["MASTER_ADDR"] = "localhost"
        os.environ["MASTER_PORT"] = "12355"
        dist.init_process_group("nccl", rank=rank, world_size=n_devices)
    if rank == 0:
        print("Setting up GNN...")
    acc = Accuracy(task="multiclass", num_classes=data.num_classes)
    model = GraphSAGE(
        in_channels=data["paper"].x.size(-1),
        hidden_channels=hidden_channels,
        num_layers=len(sizes),
        out_channels=data.num_classes,
        dropout=dropout,
        data=data,
    )
    # store node IDs for embeddings
    data['author'].n_id = torch.arange(data['author'].num_nodes).reshape(-1, 1)
    data['institution'].n_id = torch.arange(
        data['institution'].num_nodes).reshape(-1, 1)

    if rank == 0:
        print(f"# GNN Params: \
            {sum([p.numel() for p in model.parameters()])/10**6:.1f}M")
        print('Setting up NeighborLoaders...')
    train_idx = data["paper"].train_mask.nonzero(as_tuple=False).view(-1)
    eval_idx = data["paper"].val_mask.nonzero(as_tuple=False).view(-1)
    test_idx = data["paper"].test_mask.nonzero(as_tuple=False).view(-1)
    if n_devices > 1:
        # Split indices into `n_devices` many chunks:
        train_idx = train_idx.split(train_idx.size(0) // n_devices)[rank]
        eval_idx = eval_idx.split(eval_idx.size(0) // n_devices)[rank]
        test_idx = test_idx.split(test_idx.size(0) // n_devices)[rank]

    kwargs = dict(
        batch_size=batch_size,
        num_workers=16,
        persistent_workers=True,
        shuffle=True,
    )
    train_loader = NeighborLoader(
        data,
        input_nodes=("paper", train_idx),
        num_neighbors=sizes,
        drop_last=True,
        **kwargs,
    )

    eval_loader = NeighborLoader(
        data,
        input_nodes=("paper", eval_idx),
        num_neighbors=sizes,
        **kwargs,
    )
    test_loader = NeighborLoader(
        data,
        input_nodes=("paper", test_idx),
        num_neighbors=sizes,
        **kwargs,
    )
    if rank == 0:
        print("Final setup...")
    if n_devices > 0:
        model.to(rank)
    if rank == 0:
        print("about to make optimizer")
    if n_devices > 1:
        model = DistributedDataParallel(model, device_ids=[rank])
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)

    for epoch in range(1, num_epochs + 1):
        model.train()
        time_sum, sum_acc = 0, 0
        for i, batch in enumerate(train_loader):
            if num_steps_per_epoch >= 0 and i >= num_steps_per_epoch:
                break
            since = time.time()
            optimizer.zero_grad()
            if n_devices > 0:
                batch = batch.to(rank, "x", "y", "edge_index")
            loss, train_acc = training_step(batch, acc, model)
            sum_acc += train_acc
            loss.backward()
            optimizer.step()
            iter_time = time.time() - since
            if i > num_warmup_iters_for_timing - 1:
                time_sum += iter_time
            if rank == 0 and i % log_every_n_steps == 0:
                print(f"Epoch: {epoch:02d}, Step: {i:d}, Loss: {loss:.4f}, \
                    Train Acc: {sum_acc / (i) * 100.0:.2f}%, \
                    Step Time: {iter_time:.4f}s")
        if n_devices > 1:
            dist.barrier()
        print(f"Epoch: {epoch:02d}, Loss: {loss:.4f}, \
            Train Acc:{sum_acc / i * 100.0:.2f}%, \
            Average Step Time: \
            {time_sum/(i - num_warmup_iters_for_timing):.4f}s")
        model.eval()
        acc_sum = 0
        with torch.no_grad():
            for i, batch in enumerate(eval_loader):
                if i >= eval_steps:
                    break
                if n_devices > 0:
                    batch = batch.to(rank, "x", "y", "edge_index")
                acc_sum += validation_step(batch, acc, model)
            torch.distributed.all_reduce(acc_sum,
                                         op=torch.distributed.ReduceOp.MEAN)
            print(f"Validation Accuracy: {acc_sum/(i + 1) * 100.0:.4f}%")
    if n_devices > 1:
        dist.barrier()
    model.eval()
    acc_sum = 0
    with torch.no_grad():
        for i, batch in enumerate(test_loader):
            if i >= eval_steps:
                break
            if n_devices > 0:
                batch = batch.to(rank, "x", "y", "edge_index")
            acc_sum += validation_step(batch, acc, model)
        torch.distributed.all_reduce(acc_sum,
                                     op=torch.distributed.ReduceOp.MEAN)
        final_test_acc = acc_sum / (i + 1) * 100.0
        print(f"Test Accuracy: {final_test_acc:.4f}%", )
    if n_devices > 1:
        dist.destroy_process_group()
    torch.save(model, 'trained_graphsage_for_mag240m.pt')
    assert final_test_acc >= 68.0


if __name__ == "__main__":
    help_str = "-1 by default means run the full \
                    dataset each epoch, \
                    otherwise select how many steps to take."

    parser = argparse.ArgumentParser()
    parser.add_argument("--hidden_channels", type=int, default=1024)
    parser.add_argument("--batch_size", type=int, default=1024)
    parser.add_argument("--dropout", type=float, default=0.5)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--num_steps_per_epoch", type=int, default=-1,
                        help=help_str)
    parser.add_argument("--log_every_n_steps", type=int, default=1)
    parser.add_argument("--eval_steps", type=int, default=100)
    parser.add_argument("--num_warmup_iters_for_timing", type=int, default=100)
    parser.add_argument(
        "--subgraph", type=float, default=1,
        help='decimal from (0,1] representing the \
        portion of nodes to use in subgraph')
    parser.add_argument("--sizes", type=str, default="25-15")
    parser.add_argument("--n_devices", type=int, default=1,
                        help="0 devices for CPU, or 1-8 to use GPUs")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    args.sizes = [int(i) for i in args.sizes.split("-")]
    print(args)
    if not args.debug:
        import warnings

        warnings.simplefilter("ignore")
    if not torch.cuda.is_available():
        print("No GPUs available, running with CPU")
        args.n_devices = 0
    if args.n_devices > torch.cuda.device_count():
        print(
            args.n_devices,
            "GPUs requested but only",
            torch.cuda.device_count(),
            "GPUs available",
        )
        args.n_devices = torch.cuda.device_count()
    print("Loading Data...")
    dataset = MAG240MDataset()
    data = dataset.to_pyg_hetero_data()
    print("Data =", data)

    if args.subgraph < 1.0:
        print("Making a subgraph of the data to \
            save and reduce hardware requirements...")
        data = data.subgraph({
            n_type:
            torch.randperm(
                data[n_type].num_nodes)[:int(data[n_type].num_nodes *
                                             args.subgraph)]
            for n_type in data.node_types
        })
    if args.n_devices > 1:
        print("Let's use", args.n_devices, "GPUs!")
        from torch.multiprocessing.spawn import ProcessExitedException
        try:
            mp.spawn(
                run,
                args=(
                    data,
                    args.n_devices,
                    args.epochs,
                    args.num_steps_per_epoch,
                    args.log_every_n_steps,
                    args.batch_size,
                    args.sizes,
                    args.hidden_channels,
                    args.dropout,
                    args.eval_steps,
                    args.num_warmup_iters_for_timing,
                    args.debug,
                ),
                nprocs=args.n_devices,
                join=True,
            )
        except ProcessExitedException as e:
            print("torch.multiprocessing.spawn.ProcessExitedException:", e)
            print("Exceptions/SIGBUS/Errors may be caused by a lack of RAM")

    else:
        if args.n_devices == 1:
            print("Using a single GPU")
        else:
            print("Using CPU")
        run(
            0,
            data,
            args.n_devices,
            args.epochs,
            args.num_steps_per_epoch,
            args.log_every_n_steps,
            args.batch_size,
            args.sizes,
            args.hidden_channels,
            args.dropout,
            args.eval_steps,
            args.num_warmup_iters_for_timing,
            args.debug,
        )
