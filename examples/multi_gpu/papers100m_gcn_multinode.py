"""Multi-node multi-GPU example on ogbn-papers100m.

To run:
srun -l -N<num_nodes> --ntasks-per-node=<ngpu_per_node> \
--container-name=cont --container-image=<image_url> \
--container-mounts=/ogb-papers100m/:/workspace/dataset
python3 path_to_script.py
"""
import argparse
import os
import time

import torch
import torch.distributed as dist
import torch.nn.functional as F
from ogb.nodeproppred import PygNodePropPredDataset
from torch.nn.parallel import DistributedDataParallel
from torchmetrics import Accuracy

import torch_geometric


def get_num_workers() -> int:
    num_workers = None
    if hasattr(os, "sched_getaffinity"):
        try:
            num_workers = len(os.sched_getaffinity(0)) // 2
        except Exception:
            pass
    if num_workers is None:
        num_workers = os.cpu_count() // 2
    return num_workers


def run_train(data, world_size, model, epochs, batch_size, fan_out, split_idx,
              num_classes, cugraph_data_loader):
    local_id = int(os.environ['LOCAL_RANK'])
    rank = torch.distributed.get_rank()
    torch.cuda.set_device(local_id)
    device = torch.device(local_id)
    if rank == 0:
        print("Data =", data)
        print('Using', world_size, 'GPUs...')
    split_idx['train'] = split_idx['train'].split(
        split_idx['train'].size(0) // world_size, dim=0)[rank].clone()
    model = model.to(device)
    model = DistributedDataParallel(model, device_ids=[local_id])
    optimizer = torch.optim.Adam(model.parameters(), lr=0.01,
                                 weight_decay=0.0005)
    kwargs = dict(
        num_neighbors=[fan_out, fan_out],
        batch_size=batch_size,
    )
    # Set Up Neighbor Loading
    if cugraph_data_loader:
        import cugraph
        from cugraph_pyg.data import CuGraphStore
        from cugraph_pyg.loader import CuGraphNeighborLoader
        G = {("N", "E", "N"): data.edge_index}
        N = {"N": data.num_nodes}
        fs = cugraph.gnn.FeatureStore(backend="torch")
        fs.add_data(data.x, "N", "x")
        fs.add_data(data.y, "N", "y")
        cugraph_store = CuGraphStore(fs, G, N)
        train_loader = CuGraphNeighborLoader(
            cugraph_store,
            input_nodes=split_idx['train'],
            shuffle=True, drop_last=True,
            **kwargs)
        eval_loader = CuGraphNeighborLoader(cugraph_store,
                                            input_nodes=split_idx['valid'],
                                            **kwargs)
        test_loader = CuGraphNeighborLoader(cugraph_store,
                                            input_nodes=split_idx['test'],
                                            **kwargs)
    else:
        NeighborLoader = torch_geometric.loader.NeighborLoader
        num_work = get_num_workers()
        train_loader = NeighborLoader(data, input_nodes=split_idx['train'],
                                      num_workers=num_work, shuffle=True,
                                      drop_last=True, **kwargs)
        eval_loader = NeighborLoader(data, input_nodes=split_idx['valid'],
                                     num_workers=num_work, **kwargs)
        test_loader = NeighborLoader(data, input_nodes=split_idx['test'],
                                     num_workers=num_work, **kwargs)

    eval_steps = 1000
    warmup_steps = 20
    acc = Accuracy(task="multiclass", num_classes=num_classes).to(device)
    if rank == 0:
        print("Beginning training...")
    for epoch in range(epochs):
        for i, batch in enumerate(train_loader):
            if i == warmup_steps:
                start = time.time()
            batch = batch.to(device)
            batch.y = batch.y.to(torch.long)
            optimizer.zero_grad()
            out = model(batch.x, batch.edge_index)
            loss = F.cross_entropy(out[:batch_size], batch.y[:batch_size])
            loss.backward()
            optimizer.step()
            if rank == 0 and i % 10 == 0:
                print("Epoch: " + str(epoch) + ", Iteration: " + str(i) +
                      ", Loss: " + str(loss))
            print("Average Training Iteration Time:",
                  (time.time() - start) / (i - 10), "s/iter")
        acc_sum = 0.0
        with torch.no_grad():
            for i, batch in enumerate(eval_loader):
                if i >= eval_steps:
                    break
                batch = batch.to(device)
                batch.y = batch.y.to(torch.long)
                out = model(batch.x, batch.edge_index)
                acc_sum += acc(out[:batch_size].softmax(dim=-1),
                               batch.y[:batch_size])
            torch.distributed.all_reduce(acc_sum,
                                         op=torch.distributed.ReduceOp.MEAN)
            print(f"Validation Accuracy: {acc_sum/(i) * 100.0:.4f}%", )
    acc_sum = 0.0
    with torch.no_grad():
        for i, batch in enumerate(test_loader):
            batch = batch.to(device)
            batch.y = batch.y.to(torch.long)
            out = model(batch.x, batch.edge_index)
            acc_sum += acc(out[:batch_size].softmax(dim=-1),
                           batch.y[:batch_size])
        torch.distributed.all_reduce(acc_sum,
                                     op=torch.distributed.ReduceOp.MEAN)
        print(f"Test Accuracy: {acc_sum/(i) * 100.0:.4f}%", )
    if rank == 0:
        print("Total Program Runtime =",
              round(time.perf_counter() - wall_clock_start, 2), "seconds")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--hidden_channels', type=int, default=128)
    parser.add_argument('--num_layers', type=int, default=2)
    parser.add_argument('--lr', type=float, default=0.001)
    parser.add_argument('--epochs', type=int, default=3)
    parser.add_argument('--batch_size', type=int, default=2048)
    parser.add_argument('--fan_out', type=int, default=16)
    parser.add_argument(
        "--use_gat_conv",
        action='store_true',
        help="Wether or not to use GATConv. (Defaults to using GCNConv)",
    )
    parser.add_argument(
        "--n_gat_conv_heads",
        type=int,
        default=4,
        help="If using GATConv, number of attention heads to use",
    )
    parser.add_argument(
        "--cugraph_data_loader",
        action='store_true',
        help="Wether or not to use CuGraph for Neighbor Loading. \
            \nNote that this requires more GPU memory or \
            a reduction in batch_size/fan_out/hidden_channels/num_layers",
    )

    args = parser.parse_args()
    wall_clock_start = time.perf_counter()
    if args.cugraph_data_loader:
        from cugraph.testing.mg_utils import enable_spilling
        enable_spilling()
    # Setup multi-node:
    torch.distributed.init_process_group("nccl")
    nprocs = dist.get_world_size()
    assert dist.is_initialized(), "Distributed cluster not initialized"
    dataset = PygNodePropPredDataset(name='ogbn-papers100M')
    split_idx = dataset.get_idx_split()

    data = dataset[0]
    data.y = data.y.reshape(-1)
    if args.use_gat_conv:
        model = torch_geometric.nn.models.GAT(dataset.num_features,
                                              args.hidden_channels,
                                              args.num_layers,
                                              dataset.num_classes,
                                              heads=args.n_gat_conv_heads)
    else:
        model = torch_geometric.nn.models.GCN(dataset.num_features,
                                              args.hidden_channels,
                                              args.num_layers,
                                              dataset.num_classes)
    run_train(data, nprocs, model, args.epochs, args.batch_size, args.fan_out,
              split_idx, dataset.num_classes, args.cugraph_data_loader,
              wall_clock_start)
