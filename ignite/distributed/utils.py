import socket
from functools import wraps
from numbers import Number
from typing import Optional, Tuple, Union

import torch
import torch.distributed as dist

from ignite.distributed.comp_models import _SerialModel, has_xla_support, registered_computation_models
from ignite.utils import setup_logger

__all__ = [
    "backend",
    "device",
    "available_backends",
    "model_name",
    "get_world_size",
    "get_rank",
    "get_local_rank",
    "get_ntasks_per_node",
    "get_node_rank",
    "get_num_nodes",
    "spawn",
    "initialize",
    "finalize",
    "show_config",
    "set_local_rank",
    "all_reduce",
    "all_gather",
    "hostname",
    "has_xla_support",
    "sync",
    "registered_computation_models",
]

_model = _SerialModel()

_need_to_sync = True


def _sync_model_wrapper(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if isinstance(_model, _SerialModel) and _need_to_sync:
            sync()
        return func(*args, **kwargs)

    return wrapper


def sync():
    """Helper method to force this module to synchronize with current distributed context.
    This method should be used when distributed context is manually created or destroyed.
    """
    global _model

    for comp_model_cls in registered_computation_models:
        if comp_model_cls == _SerialModel:
            continue
        model = comp_model_cls.create_from_context()
        if model is not None:
            _model = model
            return

    _model = _SerialModel()


@_sync_model_wrapper
def device() -> Union[torch.device, str]:
    """Returns current device according to current distributed configuration.

    - `cpu` if no distributed configuration or native gloo distributed configuration
    - `cuda:local_rank` if native nccl distributed configuration
    - `xla` device if XLA distributed configuration

    Returns:
        torch.device or str
    """
    return _model.device()


@_sync_model_wrapper
def backend() -> Optional[str]:
    """Returns computation model's backend.

    - `None` for no distributed configuration
    - "nccl" or "gloo" or "mpi" for native torch distributed configuration
    - "xla-tpu" for XLA distributed configuration

    Returns:
        str or None
    """
    return _model.backend()


def available_backends() -> Tuple[str]:
    """Returns available backends.
    """
    out = ()
    for m in registered_computation_models:
        out += m.available_backends
    return out


@_sync_model_wrapper
def model_name() -> str:
    """Returns distributed configuration name (given by ignite)

    - `serial` for no distributed configuration
    - `native-dist` for native torch distributed configuration
    - `xla-dist` for XLA distributed configuration

    """
    return _model.name


@_sync_model_wrapper
def get_world_size() -> int:
    """Returns world size of current distributed configuration. Returns 1 if no distributed configuration.
    """
    return _model.get_world_size()


@_sync_model_wrapper
def get_rank() -> int:
    """Returns process rank within current distributed configuration. Returns 0 if no distributed configuration.
    """
    return _model.get_rank()


@_sync_model_wrapper
def get_local_rank() -> int:
    """Returns local process rank within current distributed configuration. Returns 0 if no distributed configuration.
    """
    return _model.get_local_rank()


@_sync_model_wrapper
def get_ntasks_per_node() -> int:
    """Returns number of processes (or tasks) per node within current distributed configuration.
    Returns 1 if no distributed configuration.
    """
    return _model.get_ntasks_per_node()


@_sync_model_wrapper
def get_num_nodes() -> int:
    """Returns number of nodes within current distributed configuration.
    Returns 1 if no distributed configuration.
    """
    return _model.get_num_nodes()


@_sync_model_wrapper
def get_node_rank() -> int:
    """Returns node rank within current distributed configuration.
    Returns 0 if no distributed configuration.
    """
    return _model.get_node_rank()


def hostname() -> str:
    """Returns host name for current process within current distributed configuration.
    """
    return socket.gethostname()


def spawn(backend, fn, args, num_procs_per_node, **kwargs):
    """Spawns `num_procs_per_node` processes that run `fn` with `args` and initialize distributed configuration
    defined by `backend`.

    Examples:

        1) Launch single node multi-GPU training

        .. code-block:: python

            # >>> python main.py

            # main.py

            import ignite.distributed as idist

            def train_fn(local_rank, a, b, c):
                import torch.distributed as dist
                assert dist.is_available() and dist.is_initialized()
                assert dist.get_world_size() == 4

                device = idist.device()
                assert device == "cuda:{}".format(local_rank)


            idist.spawn("nccl", train_fn, args=(a, b, c), num_procs_per_node=4)


        2) Launch multi-node multi-GPU training

        .. code-block:: python

            # >>> (node 0): python main.py --node_rank=0 --num_nodes=8 --master_addr=master --master_port=2222
            # >>> (node 1): python main.py --node_rank=1 --num_nodes=8 --master_addr=master --master_port=2222
            # >>> ...
            # >>> (node 7): python main.py --node_rank=7 --num_nodes=8 --master_addr=master --master_port=2222

            # main.py

            import torch
            import ignite.distributed as idist

            def train_fn(local_rank, num_nodes, num_procs_per_node):
                import torch.distributed as dist
                assert dist.is_available() and dist.is_initialized()
                assert dist.get_world_size() == num_nodes * num_procs_per_node

                device = idist.device()
                assert device == "cuda:{}".format(local_rank)

            idist.spawn(
                "nccl",
                train_fn,
                args=(num_nodes, num_procs_per_node),
                num_procs_per_node=num_procs_per_node,
                num_nodes=num_nodes,
                node_rank=node_rank,
                master_addr=master_addr,
                master_port=master_port
            )

        3) Launch single node multi-TPU training (for example on Google Colab)

        .. code-block:: python

            # >>> python main.py

            # main.py

            import ignite.distributed as idist

            def train_fn(local_rank, a, b, c):
                import torch_xla.core.xla_model as xm
                assert xm.get_world_size() == 8

                device = idist.device()
                assert "xla" in device.type


            idist.spawn("xla-tpu", train_fn, args=(a, b, c), num_procs_per_node=8)

    Args:
        backend (str): backend: `nccl`, `gloo`, `xla-tpu`
        fn (function): function to called as the entrypoint of the spawned process.
            This function must be defined at the top level of a module so it can be pickled and spawned.
            This is a requirement imposed by multiprocessing. The function is called as `fn(i, *args)`, where `i` is
            the process index and args is the passed through tuple of arguments.
        args (tuple): arguments passed to `fn`
        num_procs_per_node (int): number of processes to spawn on a single node.
        **kwargs: acceptable kwargs according to provided backend:

            - "nccl" or "gloo" : `num_nodes` (=1), `node_rank` (=0), `master_addr` (0.0.0.0), `master_port` (2222)

            - "xla-tpu" : `num_nodes` (=1), `node_rank` (=0)

    """
    _assert_backend(backend)
    for comp_model_cls in registered_computation_models:
        if backend not in comp_model_cls.available_backends:
            continue
        comp_model_cls.spawn(fn, args=args, num_procs_per_node=num_procs_per_node, backend=backend, **kwargs)


@_sync_model_wrapper
def all_reduce(tensor: Union[torch.Tensor, Number], op: str = "SUM") -> Union[torch.Tensor, Number]:
    """Helper method to perform all reduce operation.

    Args:
        tensor (torch.Tensor or number): tensor or number to collect across participating processes.
        op (str): reduction operation, "SUM" by default. Possible values: "SUM", "PRODUCT", "MIN", "MAX", "AND", "OR".

    Returns:
        torch.Tensor or number

    """
    return _model.all_reduce(tensor, op)


@_sync_model_wrapper
def all_gather(tensor: Union[torch.Tensor, Number]) -> torch.Tensor:
    """Helper method to perform all gather operation.

    Args:
        tensor (torch.Tensor or number): tensor or number to collect across participating processes.

    Returns:
        torch.Tensor of shape `(world_size * tensor.shape[0], tensor.shape[1], ...)`

    """
    return _model.all_gather(tensor)


def set_local_rank(index: int):
    """Method to hint the local rank in case if torch native distributed context is created by user
    without using :meth:`~ignite.distributed.utils.initialize` or :meth:`~ignite.distributed.utils.spawn`.

    Usage:

        User set up torch native distributed process group

        .. code-block:: python

            import ignite.distributed as idist

            def run(local_rank, *args, **kwargs):

                idist.set_local_rank(local_rank)
                # ...
                dist.init_process_group(**dist_info)
                # ...

    Args:
        index (int): local rank or current process index

    """
    from ignite.distributed.comp_models.base import ComputationModel

    ComputationModel._ext_local_rank = index


def _set_model(model):
    global _model
    _model = model


def _assert_backend(backend):
    backends = available_backends()
    if backend not in backends:
        raise ValueError("Backend should be one of '{}'".format(backends))


def initialize(backend: str, **kwargs):
    """Initializes distributed configuration according to provided `backend`

    Examples:

        Launch single node multi-GPU training with `torch.distributed.launch` utility.

        .. code-block:: python

            # >>> python -m torch.distributed.launch --nproc_per_node=4 main.py

            # main.py

            import ignite.distributed as idist

            def train_fn(local_rank, a, b, c):
                import torch.distributed as dist
                assert dist.is_available() and dist.is_initialized()
                assert dist.get_world_size() == 4

                device = idist.device()
                assert device == "cuda:{}".format(local_rank)


            idist.initialize("nccl")
            local_rank = idist.get_local_rank()
            train_fn(local_rank, a, b, c)
            idist.finalize()


    Args:
        backend (str, optional): backend: `nccl`, `gloo`, `xla-tpu`.
        **kwargs: acceptable kwargs according to provided backend:

            - "nccl" or "gloo" : timeout(=timedelta(minutes=30))


    """
    global _model, _need_to_sync

    if not (has_xla_support or dist.is_available()):
        # nothing to do => serial model
        # maybe warn about this
        return

    _assert_backend(backend)

    for comp_model_cls in registered_computation_models:
        if backend not in comp_model_cls.available_backends:
            continue
        _model = comp_model_cls(backend, **kwargs)

    _need_to_sync = False


def finalize():
    """Finalizes distributed configuration. For example, in case of native pytorch distributed configuration,
    it calls `dist.destroy_process_group()`.
    """
    global _need_to_sync
    _model.finalize()
    _need_to_sync = True


def show_config():
    """Helper method to display distributed configuration via `logging`.
    """

    # setup parallel logger
    logger = setup_logger(__name__)

    logger.info("distributed configuration: {}".format(model_name()))
    logger.info("backend: {}".format(backend()))
    logger.info("device: {}".format(device()))
    logger.info("hostname: {}".format(hostname()))
    logger.info("world size: {}".format(get_world_size()))
    logger.info("rank: {}".format(get_rank()))
    logger.info("local rank: {}".format(get_local_rank()))
    logger.info("num tasks per_node: {}".format(get_ntasks_per_node()))
    logger.info("num nodes: {}".format(get_num_nodes()))
    logger.info("node rank: {}".format(get_node_rank()))
