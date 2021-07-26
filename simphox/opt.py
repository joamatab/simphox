from collections import defaultdict

from .fdfd import FDFD
from .typing import Optional, Callable, Union, List, Tuple, Dict

import jax
import jax.numpy as jnp
from jax.config import config
from jax.experimental.optimizers import adam
import numpy as np
import holoviews as hv
from holoviews.streams import Pipe
import panel as pn
import dataclasses
import xarray

from .viz import scalar_metrics_viz

config.parse_flags_with_absl()


@dataclasses.dataclass
class OptProblem:
    """An optimization problem

    An optimization problem consists of a neural network defined at least by input parameters :code:`rho`,
    the transform function :code:`T(rho)` (default identity),
    and objective function :code:`C(T(rho))`, which maps to a scalar.
    For use with an inverse design problem (the primary use case in this module), the user can include an
    FDFD simulation and a source (to be fed into the FDFD solver). The FDFD simulation and source are then
    used to define a function :code:`S(eps) == S(T(rho))` that solves the FDFD problem where `eps == T(rho)`,
    in which case the objective function evaluates :code:`C(S(T(rho)))`.

    Args:
        transform_fn: The JAX-transformable transform function to yield epsilon (identity if None,
                    must be a single :code:`transform_fn` (to be broadcast to all)
                    or a list to match the FDFD objects respectively). Examples of transform_fn
                    could be smoothing functions, symmetry functions, and more (which can be compounded appropriately).
        cost_fn: The JAX-transformable cost function (or tuple of such functions)
            corresponding to src that takes in output of solve_fn from :code:`opt_solver`.
        fdfd: FDFD(s) used to generate the solver (FDFD is not run is :code:`fdfd` is :code:`None`)
        source: A numpy array source (FDFD is not run is :code:`source` is :code:`None`)
        metrics_fn: A metric_fn that returns useful dictionary data based on fields and FDFD object
         at certain time intervals (specified in opt). Each problem is supplied this metric_fn
         (Optional, ignored if :code:`None`).

    """
    transform_fn: Callable
    cost_fn: Callable
    fdfd: FDFD
    source: np.ndarray
    metrics_fn: Optional[Callable[[np.ndarray, FDFD], Dict]] = None


@dataclasses.dataclass
class OptViz:
    """An optimization visualization object

    An optimization visualization object consists of a plot for monitoring the
    history and current state of an optimization in real time.

    Args:
        cost_dmap: Cost dynamic map for streaming cost fn over time
        simulations_panel: Simulations panel for visualizing simulation results from last iteration
        costs_pipe: Costs pipe for streaming cost fn over time
        simulations_pipes: Simulations pipes of form :code:`eps, field, power`
            for visualizing simulation results from last iteration
        metrics_panels: Metrics panels for streaming metrics over time for each simulation (e.g. powers/power ratios)
        metrics_pipes: Metrics pipes for streaming metrics over time for each simulation
        metric_config: Metric config (a dictionary that describes how to plot/group the real-time metrics)

    """
    cost_dmap: hv.DynamicMap
    simulations_panels: Dict[str, pn.layout.Panel]
    costs_pipe: Pipe
    simulations_pipes: Dict[str, Tuple[Pipe, Pipe, Pipe]]
    metric_config: Optional[Dict[str, List[str]]] = None
    metrics_panels: Optional[Dict[str, hv.DynamicMap]] = None
    metrics_pipes: Optional[Dict[str, Dict[str, Pipe]]] = None


def opt_run(opt_problem: Union[OptProblem, List[OptProblem]], init_params: np.ndarray, num_iters: int,
            pbar: Optional[Callable] = None, step_size: float = 1, viz_interval: int = 0, metric_interval: int = 0,
            viz: Optional[OptViz] = None, backend: str = 'cpu',
            record_param_interval: int = 0) -> Tuple[np.ndarray, jnp.ndarray, Dict[str, list]]:
    """Run the optimization, which can be done over multipe simulations as long as those simulations
    share the same set of params initialized by :code:`init_params`.

        Args:
            opt_problem: An :code:`OptProblem` or list of :code:`OptProblem`'s. If a list is provided,
                the optimization optimizes the sum of all objective functions.
                If the user wants to weight the objective functions, weights must be inlcuded in the objective function
                definition itself, but we may provide support for this feature at a later time if needed.
            init_params: Initial parameters for the optimizer (:code:`eps` if :code:`None`)
            num_iters: Number of iterations to run
            pbar: Progress bar to keep track of optimization progress with ideally a simple tqdm interface
            step_size: For the Adam update, specify the step size needed.
            viz_interval: The optimization intermediate results are recorded every :code:`record_interval` steps
                (default of 0 means do not visualize anything)
            metric_interval: The interval over which a recorded object (e.g. metric, param)
             are recorded in a given :code:`OptProblem` (default of 0 means do not record anything).
            viz: The :code:`OptViz` object required for visualizing the optimization in real time.
            backend: Recommended backend for :code:`ndim == 2` is :code:`'cpu'` and :code:`ndim == 3` is :code:`'gpu'`
            record_param_interval: Whether to record the parameter metric at the specified :code:`record_interval`.
                Beware, this can use up a lot of memory so use judiciously.

        Returns:
            A tuple of the final eps distribution (:code:`transform_fn(p)`) and parameters :code:`p`

    """

    opt_init, opt_update, get_params = adam(step_size=step_size)
    opt_state = opt_init(init_params)

    # define opt_problems
    opt_problems = [opt_problem] if isinstance(opt_problem, OptProblem) else opt_problem

    # opt problems that include both an FDFD sim and a source sim
    sim_opt_problems = [op for op in opt_problems if op.fdfd is not None and op.source is not None]

    if viz is not None:
        if not len(viz.simulations_pipes) == len(sim_opt_problems):
            raise ValueError("Number of viz_pipes must match number of opt problems")

    # Define the objective function acting on parameters rho
    solve_fn = [None if (op.source is None or op.fdfd is None) else op.fdfd.get_opt_solve(op.source, op.transform_fn)
                for op in opt_problems]

    def overall_cost_fn(rho: jnp.ndarray):
        evals = [op.cost_fn(s(rho)) if s is not None else op.cost_fn(rho) for op, s in zip(opt_problems, solve_fn)]
        return jnp.array([obj for obj, _ in evals]).sum(), [aux for _, aux in evals]

    # Define a compiled update step
    def step_(i, opt_state):
        vaux, g = jax.value_and_grad(overall_cost_fn, has_aux=True)(get_params(opt_state))
        v, aux = vaux
        return v, opt_update(i, g, opt_state), aux

    def _update_eps(state):
        rho = get_params(state)
        for op in opt_problems:
            op.fdfd.eps = np.asarray(op.transform_fn(rho))

    step = jax.jit(step_, backend=backend)

    iterator = pbar(range(num_iters)) if pbar is not None else range(num_iters)

    costs = []
    history = defaultdict(list)

    for i in iterator:
        v, opt_state, fields = step(i, opt_state)
        if viz_interval > 0 and i % viz_interval == 0:
            _update_eps(opt_state)
            if viz.simulations_pipes is not None:
                for sop, h in zip(sim_opt_problems, fields):
                    fdfd = sop.fdfd
                    eps_pipe, field_pipe, power_pipe = viz.simulations_pipes[fdfd.name]
                    eps_pipe.send((fdfd.eps.T - np.min(fdfd.eps)) / (np.max(fdfd.eps) - np.min(fdfd.eps)))
                    hz = np.reshape(np.asarray(h), fdfd.shape).squeeze().T
                    power = np.abs(hz) ** 2
                    field_pipe.send(hz.real / np.max(hz.real))
                    power_pipe.send(power / np.max(power))
        if metric_interval > 0 and i % metric_interval == 0 and viz.metrics_pipes is not None:
            for sop, h in zip(sim_opt_problems, fields):
                metrics = sop.metrics_fn(h, sop.fdfd)
                for metric_name, metric_value in metrics.items():
                    history[f'{metric_name}/{sop.fdfd.name}'].append(metric_value)
                for title in viz.metrics_pipes[sop.fdfd.name]:
                    viz.metrics_pipes[sop.fdfd.name][title].send(
                        xarray.DataArray(
                            data=np.asarray([history[f'{metric_name}/{sop.fdfd.name}']
                                             for metric_name in viz.metric_config[title]]),
                            coords={
                                'metric': viz.metric_config[title],
                                'iteration': np.arange(i + 1)
                            },
                            dims=['metric', 'iteration'],
                            name=title
                        )
                    )
        if record_param_interval > 0 and i % record_param_interval == 0:
            for sop in sim_opt_problems:
                history[f'eps/{sop.fdfd.name}'].append((i, sop.fdfd.eps))
        iterator.set_description(f"𝓛: {v:.5f}")
        costs.append(v)
        if viz.costs_pipe is not None:
            viz.costs_pipe.send(np.asarray(costs))
    _update_eps(opt_state)
    return np.asarray(costs), get_params(opt_state), dict(history)


def opt_viz(opt_problem: Union[OptProblem, List[OptProblem]], metric_config: Dict[str, List[str]]) -> OptViz:
    """Optimization visualization panel

    Args:
        opt_problem: An :code:`OptProblem` or list of :code:`OptProblem`'s.
        metric_config: A dictionary of titles mapped to lists of metrics to plot in the graph (for overlay)

    Returns:
        A tuple of visualization panel, loss curve pipe, and visualization pipes

    """
    opt_problems = [opt_problem] if isinstance(opt_problem, OptProblem) else opt_problem
    viz_panel_pipes = {op.fdfd.name: op.fdfd.viz_panel()
                       for op in opt_problems if op.fdfd is not None and op.source is not None}
    costs_pipe = Pipe(data=[])

    metrics_panel_pipes = {op.fdfd.name: scalar_metrics_viz(metric_config=metric_config)
                           for op in opt_problems if op.fdfd is not None and op.source is not None}

    return OptViz(
        cost_dmap=hv.DynamicMap(hv.Curve, streams=[costs_pipe]).opts(title='Cost Fn (𝓛)'),
        simulations_panels={name: v[0] for name, v in viz_panel_pipes.items()},
        costs_pipe=costs_pipe,
        simulations_pipes={name: v[1] for name, v in viz_panel_pipes.items()},
        metrics_panels={name: m[0] for name, m in metrics_panel_pipes.items()},
        metrics_pipes={name: m[1] for name, m in metrics_panel_pipes.items()},
        metric_config=metric_config
    )