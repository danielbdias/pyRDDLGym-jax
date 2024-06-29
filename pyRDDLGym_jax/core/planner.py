from ast import literal_eval
from collections import deque
import configparser
from enum import Enum
import haiku as hk
import jax
import jax.numpy as jnp
import jax.random as random
import jax.nn.initializers as initializers
import numpy as np
import optax
import os
import sys
import termcolor
import time
from tqdm import tqdm
from typing import Callable, Dict, Generator, Optional, Set, Sequence, Tuple

from pyRDDLGym.core.debug.exception import raise_warning

# try to import matplotlib, if failed then skip plotting
try:
    import matplotlib
    import matplotlib.pyplot as plt
    matplotlib.use('TkAgg')
except Exception:
    raise_warning('matplotlib is not installed, '
                  'plotting functionality is disabled.', 'red')
    plt = None
            
from pyRDDLGym.core.compiler.model import RDDLPlanningModel, RDDLLiftedModel
from pyRDDLGym.core.debug.logger import Logger
from pyRDDLGym.core.debug.exception import (
    RDDLNotImplementedError,
    RDDLUndefinedVariableError,
    RDDLTypeError
)
from pyRDDLGym.core.policy import BaseAgent

from pyRDDLGym_jax.core.compiler import JaxRDDLCompiler
from pyRDDLGym_jax.core import logic
from pyRDDLGym_jax.core.logic import FuzzyLogic


# ***********************************************************************
# CONFIG FILE MANAGEMENT
# 
# - read config files from file path
# - extract experiment settings
# - instantiate planner
#
# ***********************************************************************

def _parse_config_file(path: str):
    if not os.path.isfile(path):
        raise FileNotFoundError(f'File {path} does not exist.')
    config = configparser.RawConfigParser()
    config.optionxform = str 
    config.read(path)
    args = {k: literal_eval(v) 
            for section in config.sections()
            for (k, v) in config.items(section)}
    return config, args


def _parse_config_string(value: str):
    config = configparser.RawConfigParser()
    config.optionxform = str 
    config.read_string(value)
    args = {k: literal_eval(v) 
            for section in config.sections()
            for (k, v) in config.items(section)}
    return config, args


def _load_config(config, args):
    model_args = {k: args[k] for (k, _) in config.items('Model')}
    planner_args = {k: args[k] for (k, _) in config.items('Optimizer')}
    train_args = {k: args[k] for (k, _) in config.items('Training')}    
    
    train_args['key'] = jax.random.PRNGKey(train_args['key'])
    
    # read the model settings
    logic_name = model_args.get('logic', 'FuzzyLogic')
    logic_kwargs = model_args.get('logic_kwargs', {})
    tnorm_name = model_args.get('tnorm', 'ProductTNorm')
    tnorm_kwargs = model_args.get('tnorm_kwargs', {})
    comp_name = model_args.get('complement', 'StandardComplement')
    comp_kwargs = model_args.get('complement_kwargs', {})
    compare_name = model_args.get('comparison', 'SigmoidComparison')
    compare_kwargs = model_args.get('comparison_kwargs', {})
    logic_kwargs['tnorm'] = getattr(logic, tnorm_name)(**tnorm_kwargs)
    logic_kwargs['complement'] = getattr(logic, comp_name)(**comp_kwargs)
    logic_kwargs['comparison'] = getattr(logic, compare_name)(**compare_kwargs)
    
    # read the optimizer settings
    plan_method = planner_args.pop('method')
    plan_kwargs = planner_args.pop('method_kwargs', {})  
    
    if 'initializer' in plan_kwargs:  # weight initialization
        init_name = plan_kwargs['initializer']
        init_class = getattr(initializers, init_name)
        init_kwargs = plan_kwargs.pop('initializer_kwargs', {})
        try: 
            plan_kwargs['initializer'] = init_class(**init_kwargs)
        except Exception as _:
            raise_warning(f'Ignoring invalid arguments for initializer <{init_name}>.')
            plan_kwargs['initializer'] = init_class
               
    if 'activation' in plan_kwargs:  # activation function
        plan_kwargs['activation'] = getattr(jax.nn, plan_kwargs['activation'])
    
    planner_args['logic'] = getattr(logic, logic_name)(**logic_kwargs)
    planner_args['plan'] = getattr(sys.modules[__name__], plan_method)(**plan_kwargs)
    if 'optimizer' in planner_args:
        planner_args['optimizer'] = getattr(optax, planner_args['optimizer'])
    
    return planner_args, plan_kwargs, train_args


def load_config(path: str) -> Tuple[Dict[str, object], ...]:
    '''Loads a config file at the specified file path.'''
    config, args = _parse_config_file(path)
    return _load_config(config, args)


def load_config_from_string(value: str) -> Tuple[Dict[str, object], ...]:
    '''Loads config file contents specified explicitly as a string value.'''
    config, args = _parse_config_string(value)
    return _load_config(config, args)

    
# ***********************************************************************
# MODEL RELAXATIONS
# 
# - replace discrete ops in state dynamics/reward with differentiable ones
#
# ***********************************************************************

def _function_discrete_approx_named(logic):
    jax_discrete, jax_param = logic.discrete()

    def _jax_wrapped_discrete_calc_approx(key, prob, params):
        sample = jax_discrete(key, prob, params)
        out_of_bounds = jnp.logical_not(jnp.logical_and(
            jnp.all(prob >= 0),
            jnp.allclose(jnp.sum(prob, axis=-1), 1.0)))
        return sample, out_of_bounds
    
    return _jax_wrapped_discrete_calc_approx, jax_param


class JaxRDDLCompilerWithGrad(JaxRDDLCompiler):
    '''Compiles a RDDL AST representation to an equivalent JAX representation. 
    Unlike its parent class, this class treats all fluents as real-valued, and
    replaces all mathematical operations by equivalent ones with a well defined 
    (e.g. non-zero) gradient where appropriate. 
    '''
    
    def __init__(self, *args,
                 logic: FuzzyLogic=FuzzyLogic(),
                 cpfs_without_grad: Optional[set]=None,
                 **kwargs) -> None:
        '''Creates a new RDDL to Jax compiler, where operations that are not
        differentiable are converted to approximate forms that have defined 
        gradients.
        
        :param *args: arguments to pass to base compiler
        :param logic: Fuzzy logic object that specifies how exact operations
        are converted to their approximate forms: this class may be subclassed
        to customize these operations
        :param cpfs_without_grad: which CPFs do not have gradients (use straight
        through gradient trick)
        :param *kwargs: keyword arguments to pass to base compiler
        '''
        super(JaxRDDLCompilerWithGrad, self).__init__(*args, **kwargs)
        self.logic = logic
        self.logic.set_use64bit(self.use64bit)
        if cpfs_without_grad is None:
            cpfs_without_grad = set()
        self.cpfs_without_grad = cpfs_without_grad
        
        # actions and CPFs must be continuous
        raise_warning('Initial values of pvariables will be cast to real.')   
        for (var, values) in self.init_values.items():
            self.init_values[var] = np.asarray(values, dtype=self.REAL) 
        
        # overwrite basic operations with fuzzy ones
        self.RELATIONAL_OPS = {
            '>=': logic.greater_equal(),
            '<=': logic.less_equal(),
            '<': logic.less(),
            '>': logic.greater(),
            '==': logic.equal(),
            '~=': logic.not_equal()
        }
        self.LOGICAL_NOT = logic.logical_not()
        self.LOGICAL_OPS = {
            '^': logic.logical_and(),
            '&': logic.logical_and(),
            '|': logic.logical_or(),
            '~': logic.xor(),
            '=>': logic.implies(),
            '<=>': logic.equiv()
        }
        self.AGGREGATION_OPS['forall'] = logic.forall()
        self.AGGREGATION_OPS['exists'] = logic.exists()
        self.AGGREGATION_OPS['argmin'] = logic.argmin()
        self.AGGREGATION_OPS['argmax'] = logic.argmax()
        self.KNOWN_UNARY['sgn'] = logic.sgn()
        self.KNOWN_UNARY['floor'] = logic.floor()   
        self.KNOWN_UNARY['ceil'] = logic.ceil()   
        self.KNOWN_UNARY['round'] = logic.round()
        self.KNOWN_UNARY['sqrt'] = logic.sqrt()
        self.KNOWN_BINARY['div'] = logic.div()
        self.KNOWN_BINARY['mod'] = logic.mod()
        self.KNOWN_BINARY['fmod'] = logic.mod()
        self.IF_HELPER = logic.control_if()
        self.SWITCH_HELPER = logic.control_switch()
        self.BERNOULLI_HELPER = logic.bernoulli()
        self.DISCRETE_HELPER = _function_discrete_approx_named(logic)
        
    def _jax_stop_grad(self, jax_expr):
        
        def _jax_wrapped_stop_grad(x, params, key):
            sample, key, error = jax_expr(x, params, key)
            sample = jax.lax.stop_gradient(sample)
            return sample, key, error
        
        return _jax_wrapped_stop_grad
        
    def _compile_cpfs(self, info):
        raise_warning('CPFs outputs will be cast to real.')      
        jax_cpfs = {}
        for (_, cpfs) in self.levels.items():
            for cpf in cpfs:
                _, expr = self.rddl.cpfs[cpf]
                jax_cpfs[cpf] = self._jax(expr, info, dtype=self.REAL)
                if cpf in self.cpfs_without_grad:
                    raise_warning(f'CPF <{cpf}> stops gradient.')      
                    jax_cpfs[cpf] = self._jax_stop_grad(jax_cpfs[cpf])
        return jax_cpfs
    
    def _jax_kron(self, expr, info):
        if self.logic.verbose:
            raise_warning('KronDelta will be ignored.')            
        arg, = expr.args
        arg = self._jax(arg, info)
        return arg
    

# ***********************************************************************
# ALL VERSIONS OF JAX PLANS
# 
# - straight line plan
# - deep reactive policy
#
# ***********************************************************************

class JaxPlan:
    '''Base class for all JAX policy representations.'''
    
    def __init__(self) -> None:
        self._initializer = None
        self._train_policy = None
        self._test_policy = None
        self._projection = None
    
    def summarize_hyperparameters(self):
        pass
        
    def compile(self, compiled: JaxRDDLCompilerWithGrad,
                _bounds: Dict,
                horizon: int) -> None:
        raise NotImplementedError
    
    def guess_next_epoch(self, params: Dict) -> Dict:
        raise NotImplementedError
    
    @property
    def initializer(self):
        return self._initializer

    @initializer.setter
    def initializer(self, value):
        self._initializer = value
    
    @property
    def train_policy(self):
        return self._train_policy

    @train_policy.setter
    def train_policy(self, value):
        self._train_policy = value
        
    @property
    def test_policy(self):
        return self._test_policy

    @test_policy.setter
    def test_policy(self, value):
        self._test_policy = value
         
    @property
    def projection(self):
        return self._projection

    @projection.setter
    def projection(self, value):
        self._projection = value
    
    def _calculate_action_info(self, compiled: JaxRDDLCompilerWithGrad,
                               user_bounds: Dict[str, object], horizon: int):
        shapes, bounds, bounds_safe, cond_lists = {}, {}, {}, {}
        for (name, prange) in compiled.rddl.variable_ranges.items():
            if compiled.rddl.variable_types[name] != 'action-fluent':
                continue
            
            # check invalid type
            if prange not in compiled.JAX_TYPES:
                raise RDDLTypeError(
                    f'Invalid range <{prange}. of action-fluent <{name}>, '
                    f'must be one of {set(compiled.JAX_TYPES.keys())}.')
                
            # clip boolean to (0, 1), otherwise use the RDDL action bounds
            # or the user defined action bounds if provided
            shapes[name] = (horizon,) + np.shape(compiled.init_values[name])
            if prange == 'bool':
                lower, upper = None, None
            else:
                lower, upper = compiled.constraints.bounds[name]
                lower, upper = user_bounds.get(name, (lower, upper))
                lower = np.asarray(lower, dtype=compiled.REAL)
                upper = np.asarray(upper, dtype=compiled.REAL)
                lower_finite = np.isfinite(lower)
                upper_finite = np.isfinite(upper)
                bounds_safe[name] = (np.where(lower_finite, lower, 0.0),
                                     np.where(upper_finite, upper, 0.0))
                cond_lists[name] = [lower_finite & upper_finite,
                                    lower_finite & ~upper_finite,
                                    ~lower_finite & upper_finite,
                                    ~lower_finite & ~upper_finite]
            bounds[name] = (lower, upper)
            raise_warning(f'Bounds of action fluent <{name}> set to {bounds[name]}.')
        return shapes, bounds, bounds_safe, cond_lists
    
    def _count_bool_actions(self, rddl: RDDLLiftedModel):
        constraint = rddl.max_allowed_actions
        num_bool_actions = sum(np.size(values)
                               for (var, values) in rddl.action_fluents.items()
                               if rddl.variable_ranges[var] == 'bool')
        return num_bool_actions, constraint

    
class JaxStraightLinePlan(JaxPlan):
    '''A straight line plan implementation in JAX'''
    
    def __init__(self, initializer: initializers.Initializer=initializers.normal(),
                 wrap_sigmoid: bool=True,
                 min_action_prob: float=1e-6,
                 wrap_non_bool: bool=False,
                 wrap_softmax: bool=False,
                 use_new_projection: bool=False,
                 max_constraint_iter: int=100) -> None:
        '''Creates a new straight line plan in JAX.
        
        :param initializer: a Jax Initializer for setting the initial actions
        :param wrap_sigmoid: wrap bool action parameters with sigmoid 
        (uses gradient clipping instead of sigmoid if None; this flag is ignored
        if wrap_softmax = True)
        :param min_action_prob: minimum value a soft boolean action can take
        (maximum is 1 - min_action_prob); required positive if wrap_sigmoid = True
        :param wrap_non_bool: whether to wrap real or int action fluent parameters
        with non-linearity (e.g. sigmoid or ELU) to satisfy box constraints
        :param wrap_softmax: whether to use softmax activation approach 
        (note, this is limited to max-nondef-actions = 1) instead of projected
        gradient to satisfy action constraints 
        :param use_new_projection: whether to use non-iterative (e.g. sort-based)
        projection method, or modified SOGBOFA projection method to satisfy
        action concurrency constraint
        :param max_constraint_iter: max iterations of projected 
        gradient for ensuring actions satisfy constraints, only required if 
        use_new_projection = True
        '''
        super(JaxStraightLinePlan, self).__init__()
        self._initializer_base = initializer
        self._initializer = initializer
        self._wrap_sigmoid = wrap_sigmoid
        self._min_action_prob = min_action_prob
        self._wrap_non_bool = wrap_non_bool
        self._wrap_softmax = wrap_softmax
        self._use_new_projection = use_new_projection
        self._max_constraint_iter = max_constraint_iter
        
    def summarize_hyperparameters(self):
        print(f'policy hyper-parameters:\n'
              f'    initializer          ={type(self._initializer_base).__name__}\n'
              f'constraint-sat strategy (simple):\n'
              f'    wrap_sigmoid         ={self._wrap_sigmoid}\n'
              f'    wrap_sigmoid_min_prob={self._min_action_prob}\n'
              f'    wrap_non_bool        ={self._wrap_non_bool}\n'
              f'constraint-sat strategy (complex):\n'
              f'    wrap_softmax         ={self._wrap_softmax}\n'
              f'    use_new_projection   ={self._use_new_projection}')
    
    def compile(self, compiled: JaxRDDLCompilerWithGrad,
                _bounds: Dict, horizon: int) -> None:
        rddl = compiled.rddl
        
        # calculate the correct action box bounds
        shapes, bounds, bounds_safe, cond_lists = self._calculate_action_info(
            compiled, _bounds, horizon)
        self.bounds = bounds
        
        # action concurrency check
        bool_action_count, allowed_actions = self._count_bool_actions(rddl)
        use_constraint_satisfaction = allowed_actions < bool_action_count        
        if use_constraint_satisfaction: 
            raise_warning(f'Using projected gradient trick to satisfy '
                          f'max_nondef_actions: total boolean actions '
                          f'{bool_action_count} > max_nondef_actions '
                          f'{allowed_actions}.')
            
        noop = {var: (values[0] if isinstance(values, list) else values)
                for (var, values) in rddl.action_fluents.items()}
        bool_key = 'bool__'
        
        # ***********************************************************************
        # STRAIGHT-LINE PLAN
        #
        # ***********************************************************************
        
        # define the mapping between trainable parameter and action
        wrap_sigmoid = self._wrap_sigmoid
        bool_threshold = 0.0 if wrap_sigmoid else 0.5
        
        def _jax_bool_param_to_action(var, param, hyperparams):
            if wrap_sigmoid:
                weight = hyperparams[var]
                return jax.nn.sigmoid(weight * param)
            else:
                return param 
        
        def _jax_bool_action_to_param(var, action, hyperparams):
            if wrap_sigmoid:
                weight = hyperparams[var]
                return (-1.0 / weight) * jnp.log(1.0 / action - 1.0)
            else:
                return action
            
        wrap_non_bool = self._wrap_non_bool
        
        def _jax_non_bool_param_to_action(var, param, hyperparams):
            if wrap_non_bool:
                lower, upper = bounds_safe[var]
                action = jnp.select(
                    condlist=cond_lists[var],
                    choicelist=[
                        lower + (upper - lower) * jax.nn.sigmoid(param),
                        lower + (jax.nn.elu(param) + 1.0),
                        upper - (jax.nn.elu(-param) + 1.0),
                        param
                    ]
                )
            else:
                action = param
            return action
        
        # handle box constraints    
        min_action = self._min_action_prob
        max_action = 1.0 - min_action
        
        def _jax_project_bool_to_box(var, param, hyperparams):
            lower = _jax_bool_action_to_param(var, min_action, hyperparams)
            upper = _jax_bool_action_to_param(var, max_action, hyperparams)
            valid_param = jnp.clip(param, lower, upper)
            return valid_param
        
        ranges = rddl.variable_ranges
        
        def _jax_wrapped_slp_project_to_box(params, hyperparams):
            new_params = {}
            for (var, param) in params.items():
                if var == bool_key:
                    new_params[var] = param
                elif ranges[var] == 'bool':
                    new_params[var] = _jax_project_bool_to_box(var, param, hyperparams)
                elif wrap_non_bool:
                    new_params[var] = param
                else:
                    new_params[var] = jnp.clip(param, *bounds[var])
            return new_params, True
        
        # convert softmax action back to action dict
        action_sizes = {var: np.prod(shape[1:], dtype=int) 
                        for (var, shape) in shapes.items()
                        if ranges[var] == 'bool'}
        
        def _jax_unstack_bool_from_softmax(output):
            actions = {}
            start = 0
            for (name, size) in action_sizes.items():
                action = output[..., start:start + size]
                action = jnp.reshape(action, newshape=shapes[name][1:])
                if noop[name]:
                    action = 1.0 - action
                actions[name] = action
                start += size
            return actions
                
        # train plan prediction (TODO: implement one-hot for integer actions)        
        def _jax_wrapped_slp_predict_train(key, params, hyperparams, step, subs):
            actions = {}
            for (var, param) in params.items():
                action = jnp.asarray(param[step, ...], dtype=compiled.REAL)
                if var == bool_key:
                    output = jax.nn.softmax(action)
                    bool_actions = _jax_unstack_bool_from_softmax(output)
                    actions.update(bool_actions)
                elif ranges[var] == 'bool':
                    actions[var] = _jax_bool_param_to_action(var, action, hyperparams)
                else:
                    actions[var] = _jax_non_bool_param_to_action(var, action, hyperparams)
            return actions
        
        # test plan prediction
        def _jax_wrapped_slp_predict_test(key, params, hyperparams, step, subs):
            actions = {}
            for (var, param) in params.items():
                action = jnp.asarray(param[step, ...], dtype=compiled.REAL)
                if var == bool_key:
                    output = jax.nn.softmax(action)
                    bool_actions = _jax_unstack_bool_from_softmax(output)
                    for (bool_var, bool_action) in bool_actions.items():
                        actions[bool_var] = bool_action > 0.5
                elif ranges[var] == 'bool':
                    actions[var] = action > bool_threshold
                else:
                    action = _jax_non_bool_param_to_action(var, action, hyperparams)
                    action = jnp.clip(action, *bounds[var])
                    if ranges[var] == 'int':
                        action = jnp.round(action).astype(compiled.INT)
                    actions[var] = action
            return actions
        
        self.train_policy = _jax_wrapped_slp_predict_train
        self.test_policy = _jax_wrapped_slp_predict_test
        
        # ***********************************************************************
        # ACTION CONSTRAINT SATISFACTION
        #
        # ***********************************************************************
        
        # use a softmax output activation
        if use_constraint_satisfaction and self._wrap_softmax:
            
            # only allow one action non-noop for now
            if 1 < allowed_actions < bool_action_count:
                raise RDDLNotImplementedError(
                    f'Straight-line plans with wrap_softmax currently '
                    f'do not support max-nondef-actions = {allowed_actions} > 1.')
                
            # potentially apply projection but to non-bool actions only
            self.projection = _jax_wrapped_slp_project_to_box
            
        # use new gradient projection method...
        elif use_constraint_satisfaction and self._use_new_projection:
            
            # shift the boolean actions uniformly, clipping at the min/max values
            # the amount to move is such that only top allowed_actions actions
            # are still active (e.g. not equal to noop) after the shift
            def _jax_wrapped_sorting_project(params, hyperparams):
                
                # find the amount to shift action parameters
                # if noop is True pretend it is False and reflect the parameter
                scores = []
                for (var, param) in params.items():
                    if ranges[var] == 'bool':
                        param_flat = jnp.ravel(param)
                        if noop[var]:
                            param_flat = (-param_flat) if wrap_sigmoid else 1.0 - param_flat
                        scores.append(param_flat)
                scores = jnp.concatenate(scores)
                descending = jnp.sort(scores)[::-1]
                kplus1st_greatest = descending[allowed_actions]
                surplus = jnp.maximum(kplus1st_greatest - bool_threshold, 0.0)
                    
                # perform the shift
                new_params = {}
                for (var, param) in params.items():
                    if ranges[var] == 'bool':
                        new_param = param + (surplus if noop[var] else -surplus)
                        new_param = _jax_project_bool_to_box(var, new_param, hyperparams)
                    else:
                        new_param = param
                    new_params[var] = new_param
                return new_params, True
                
            # clip actions to valid bounds and satisfy constraint on max actions
            def _jax_wrapped_slp_project_to_max_constraint(params, hyperparams):
                params, _ = _jax_wrapped_slp_project_to_box(params, hyperparams)
                project_over_horizon = jax.vmap(
                    _jax_wrapped_sorting_project, in_axes=(0, None)
                )(params, hyperparams)
                return project_over_horizon
            
            self.projection = _jax_wrapped_slp_project_to_max_constraint
        
        # use SOGBOFA projection method...
        elif use_constraint_satisfaction and not self._use_new_projection:
            
            # calculate the surplus of actions above max-nondef-actions
            def _jax_wrapped_sogbofa_surplus(params, hyperparams):
                sum_action, count = 0.0, 0
                for (var, param) in params.items():
                    if ranges[var] == 'bool':
                        action = _jax_bool_param_to_action(var, param, hyperparams)                        
                        if noop[var]:
                            sum_action += jnp.size(action) - jnp.sum(action)
                            count += jnp.sum(action < 1)
                        else:
                            sum_action += jnp.sum(action)
                            count += jnp.sum(action > 0)
                surplus = jnp.maximum(sum_action - allowed_actions, 0.0)
                count = jnp.maximum(count, 1)
                return surplus / count
                
            # return whether the surplus is positive or reached compute limit
            max_constraint_iter = self._max_constraint_iter
        
            def _jax_wrapped_sogbofa_continue(values):
                it, _, _, surplus = values
                return jnp.logical_and(it < max_constraint_iter, surplus > 0)
                
            # reduce all bool action values by the surplus clipping at minimum
            # for no-op = True, do the opposite, i.e. increase all
            # bool action values by surplus clipping at maximum
            def _jax_wrapped_sogbofa_subtract_surplus(values):
                it, params, hyperparams, surplus = values
                new_params = {}
                for (var, param) in params.items():
                    if ranges[var] == 'bool':
                        action = _jax_bool_param_to_action(var, param, hyperparams)
                        new_action = action + (surplus if noop[var] else -surplus)
                        new_action = jnp.clip(new_action, min_action, max_action)
                        new_param = _jax_bool_action_to_param(var, new_action, hyperparams)
                    else:
                        new_param = param
                    new_params[var] = new_param
                new_surplus = _jax_wrapped_sogbofa_surplus(new_params, hyperparams)
                new_it = it + 1
                return new_it, new_params, hyperparams, new_surplus
                
            # apply the surplus to the actions until it becomes zero
            def _jax_wrapped_sogbofa_project(params, hyperparams):
                surplus = _jax_wrapped_sogbofa_surplus(params, hyperparams)
                _, params, _, surplus = jax.lax.while_loop(
                    cond_fun=_jax_wrapped_sogbofa_continue,
                    body_fun=_jax_wrapped_sogbofa_subtract_surplus,
                    init_val=(0, params, hyperparams, surplus)
                )
                converged = jnp.logical_not(surplus > 0)
                return params, converged
                
            # clip actions to valid bounds and satisfy constraint on max actions
            def _jax_wrapped_slp_project_to_max_constraint(params, hyperparams):
                params, _ = _jax_wrapped_slp_project_to_box(params, hyperparams)
                project_over_horizon = jax.vmap(
                    _jax_wrapped_sogbofa_project, in_axes=(0, None)
                )(params, hyperparams)
                return project_over_horizon
            
            self.projection = _jax_wrapped_slp_project_to_max_constraint
        
        # just project to box constraints
        else: 
            self.projection = _jax_wrapped_slp_project_to_box
            
        # ***********************************************************************
        # PLAN INITIALIZATION
        #
        # ***********************************************************************
        
        init = self._initializer
        stack_bool_params = use_constraint_satisfaction and self._wrap_softmax
        
        def _jax_wrapped_slp_init(key, hyperparams, subs):
            params = {}
            for (var, shape) in shapes.items():
                if ranges[var] != 'bool' or not stack_bool_params: 
                    key, subkey = random.split(key)
                    param = init(subkey, shape, dtype=compiled.REAL)
                    if ranges[var] == 'bool':
                        param += bool_threshold
                    params[var] = param
            if stack_bool_params:
                key, subkey = random.split(key)
                bool_shape = (horizon, bool_action_count)
                bool_param = init(subkey, bool_shape, dtype=compiled.REAL)
                params[bool_key] = bool_param
            params, _ = _jax_wrapped_slp_project_to_box(params, hyperparams)
            return params
        
        self.initializer = _jax_wrapped_slp_init
    
    @staticmethod
    @jax.jit
    def _guess_next_epoch(param):
        # "progress" the plan one step forward and set last action to second-last
        return jnp.append(param[1:, ...], param[-1:, ...], axis=0)

    def guess_next_epoch(self, params: Dict) -> Dict:
        next_fn = JaxStraightLinePlan._guess_next_epoch
        return jax.tree_map(next_fn, params)


class JaxDeepReactivePolicy(JaxPlan):
    '''A deep reactive policy network implementation in JAX.'''
    
    def __init__(self, topology: Sequence[int],
                 activation: Callable=jax.nn.relu,
                 initializer: hk.initializers.Initializer=hk.initializers.VarianceScaling(scale=2.0),
                 normalize: bool=True, 
                 normalizer_kwargs: Optional[Dict]=None,
                 wrap_non_bool: bool=False) -> None:
        '''Creates a new deep reactive policy in JAX.
        
        :param neurons: sequence consisting of the number of neurons in each
        layer of the policy
        :param activation: function to apply after each layer of the policy
        :param initializer: weight initialization
        :param normalize: whether to apply layer norm to the inputs
        :param normalizer_kwargs: if normalize is True, apply additional arguments
        to layer norm
        :param wrap_non_bool: whether to wrap real or int action fluent parameters
        with non-linearity (e.g. sigmoid or ELU) to satisfy box constraints
        '''
        super(JaxDeepReactivePolicy, self).__init__()
        self._topology = topology
        self._activations = [activation for _ in topology]
        self._initializer_base = initializer
        self._initializer = initializer
        self._normalize = normalize
        if normalizer_kwargs is None:
            normalizer_kwargs = {
                'create_offset': True, 'create_scale': True, 
                'name': 'input_norm'
            }
        self._normalizer_kwargs = normalizer_kwargs
        self._wrap_non_bool = wrap_non_bool
            
    def summarize_hyperparameters(self):
        print(f'policy hyper-parameters:\n'
              f'    topology        ={self._topology}\n'
              f'    activation_fn   ={self._activations[0].__name__}\n'
              f'    initializer     ={type(self._initializer_base).__name__}\n'
              f'    apply_layer_norm={self._normalize}\n'
              f'    layer_norm_args ={self._normalizer_kwargs}\n'
              f'    wrap_non_bool   ={self._wrap_non_bool}')
    
    def compile(self, compiled: JaxRDDLCompilerWithGrad,
                _bounds: Dict, horizon: int) -> None:
        rddl = compiled.rddl
        
        # calculate the correct action box bounds
        shapes, bounds, bounds_safe, cond_lists = self._calculate_action_info(
            compiled, _bounds, horizon)
        shapes = {var: value[1:] for (var, value) in shapes.items()}
        self.bounds = bounds
        
        # action concurrency check - only allow one action non-noop for now
        bool_action_count, allowed_actions = self._count_bool_actions(rddl)
        if 1 < allowed_actions < bool_action_count:
            raise RDDLNotImplementedError(
                f'Deep reactive policies currently do not support '
                f'max-nondef-actions = {allowed_actions} > 1.')
        use_constraint_satisfaction = allowed_actions < bool_action_count
            
        noop = {var: (values[0] if isinstance(values, list) else values)
                for (var, values) in rddl.action_fluents.items()}                   
        bool_key = 'bool__'
        
        # ***********************************************************************
        # POLICY NETWORK PREDICTION
        #
        # ***********************************************************************
                   
        ranges = rddl.variable_ranges
        normalize = self._normalize
        wrap_non_bool = self._wrap_non_bool
        init = self._initializer
        layers = list(enumerate(zip(self._topology, self._activations)))
        layer_sizes = {var: np.prod(shape, dtype=int) 
                       for (var, shape) in shapes.items()}
        layer_names = {var: f'output_{var}'.replace('-', '_') for var in shapes}
        
        # predict actions from the policy network for current state
        def _jax_wrapped_policy_network_predict(state):
            
            # apply layer norm
            if normalize:
                normalizer = hk.LayerNorm(
                    axis=-1, param_axis=-1, **self._normalizer_kwargs)
                state = normalizer(state)
            
            # feed state vector through hidden layers
            hidden = state
            for (i, (num_neuron, activation)) in layers:
                linear = hk.Linear(num_neuron, name=f'hidden_{i}', w_init=init)
                hidden = activation(linear(hidden))
            
            # each output is a linear layer reshaped to original lifted shape
            actions = {}
            for (var, size) in layer_sizes.items():
                linear = hk.Linear(size, name=layer_names[var], w_init=init)
                reshape = hk.Reshape(output_shape=shapes[var], preserve_dims=-1,
                                     name=f'reshape_{layer_names[var]}')
                output = reshape(linear(hidden))
                if not shapes[var]:
                    output = jnp.squeeze(output)
                
                # project action output to valid box constraints 
                if ranges[var] == 'bool':
                    if not use_constraint_satisfaction:
                        actions[var] = jax.nn.sigmoid(output)
                else:
                    if wrap_non_bool:
                        lower, upper = bounds_safe[var]
                        action = jnp.select(
                            condlist=cond_lists[var],
                            choicelist=[
                                lower + (upper - lower) * jax.nn.sigmoid(output),
                                lower + (jax.nn.elu(output) + 1.0),
                                upper - (jax.nn.elu(-output) + 1.0),
                                output
                            ]
                        )
                    else:
                        action = output
                    actions[var] = action
            
            # for constraint satisfaction wrap bool actions with softmax
            if use_constraint_satisfaction:
                linear = hk.Linear(
                    bool_action_count, name='output_bool', w_init=init)
                output = jax.nn.softmax(linear(hidden))
                actions[bool_key] = output
             
            return actions
        
        predict_fn = hk.transform(_jax_wrapped_policy_network_predict)
        predict_fn = hk.without_apply_rng(predict_fn)            
        
        # convert softmax action back to action dict
        def _jax_unstack_bool_from_softmax(output):
            actions = {}
            start = 0
            for (name, size) in layer_sizes.items():
                if ranges[name] == 'bool':
                    action = output[..., start:start + size]
                    action = jnp.reshape(action, newshape=shapes[name])
                    if noop[name]:
                        action = 1.0 - action
                    actions[name] = action
                    start += size
            return actions
        
        if rddl.observ_fluents:
            observed_vars = rddl.observ_fluents
        else:
            observed_vars = rddl.state_fluents
             
        # state is concatenated into single tensor
        def _jax_wrapped_subs_to_state(subs):
            subs = {var: value
                    for (var, value) in subs.items()
                    if var in observed_vars}
            flat_subs = jax.tree_map(jnp.ravel, subs)
            states = list(flat_subs.values())
            state = jnp.concatenate(states)
            return state
        
        # train action prediction
        def _jax_wrapped_drp_predict_train(key, params, hyperparams, step, subs):
            state = _jax_wrapped_subs_to_state(subs)
            actions = predict_fn.apply(params, state)
            if not wrap_non_bool:
                for (var, action) in actions.items():
                    if var != bool_key and ranges[var] != 'bool':
                        actions[var] = jnp.clip(action, *bounds[var])
            if use_constraint_satisfaction:
                bool_actions = _jax_unstack_bool_from_softmax(actions[bool_key])
                actions.update(bool_actions)
                del actions[bool_key]
            return actions
        
        # test action prediction
        def _jax_wrapped_drp_predict_test(key, params, hyperparams, step, subs):
            actions = _jax_wrapped_drp_predict_train(
                key, params, hyperparams, step, subs)
            new_actions = {}
            for (var, action) in actions.items():
                prange = ranges[var]
                if prange == 'bool':
                    new_action = action > 0.5
                elif prange == 'int':
                    action = jnp.clip(action, *bounds[var])
                    new_action = jnp.round(action).astype(compiled.INT)
                else:
                    new_action = jnp.clip(action, *bounds[var])
                new_actions[var] = new_action
            return new_actions
        
        self.train_policy = _jax_wrapped_drp_predict_train
        self.test_policy = _jax_wrapped_drp_predict_test
        
        # ***********************************************************************
        # ACTION CONSTRAINT SATISFACTION
        #
        # ***********************************************************************
        
        # no projection applied since the actions are already constrained
        def _jax_wrapped_drp_no_projection(params, hyperparams):
            return params, True
        
        self.projection = _jax_wrapped_drp_no_projection
    
        # ***********************************************************************
        # POLICY NETWORK INITIALIZATION
        #
        # ***********************************************************************
        
        def _jax_wrapped_drp_init(key, hyperparams, subs):
            subs = {var: value[0, ...] 
                    for (var, value) in subs.items()
                    if var in observed_vars}
            state = _jax_wrapped_subs_to_state(subs)
            params = predict_fn.init(key, state)
            return params
        
        self.initializer = _jax_wrapped_drp_init
        
    def guess_next_epoch(self, params: Dict) -> Dict:
        return params

    
# ***********************************************************************
# ALL VERSIONS OF JAX PLANNER
# 
# - simple gradient descent based planner
# - more stable but slower line search based planner
#
# ***********************************************************************

class RollingMean:
    
    def __init__(self, window_size: int) -> None:
        self._window_size = window_size
        self._memory = deque(maxlen=window_size)
        self._total = 0
    
    def update(self, x: float) -> float:
        memory = self._memory
        self._total += x
        if len(memory) == self._window_size:
            self._total -= memory.popleft()
        memory.append(x)
        return self._total / len(memory)


class JaxPlannerPlot:
    
    def __init__(self, rddl: RDDLPlanningModel, horizon: int) -> None:
        self._fig, axes = plt.subplots(1 + len(rddl.action_fluents))
        
        # prepare the loss plot
        self._loss_ax = axes[0]
        self._loss_ax.autoscale(enable=True)
        self._loss_ax.set_xlabel('decision epoch')
        self._loss_ax.set_ylabel('loss value')
        self._loss_plot = self._loss_ax.plot(
            [], [], linestyle=':', marker='o', markersize=2)[0]
        self._loss_back = self._fig.canvas.copy_from_bbox(self._loss_ax.bbox)
        
        # prepare the action plots
        self._action_ax = {name: axes[idx + 1]
                           for (idx, name) in enumerate(rddl.action_fluents)}
        self._action_plots = {}
        for name in rddl.action_fluents:
            ax = self._action_ax[name]
            if rddl.variable_ranges[name] == 'bool':
                vmin, vmax = 0.0, 1.0
            else:
                vmin, vmax = None, None  
            action_dim = 1
            for dim in rddl.object_counts(rddl.variable_params[name]):
                action_dim *= dim     
            action_plot = ax.pcolormesh(
                np.zeros((action_dim, horizon)), 
                cmap='seismic', vmin=vmin, vmax=vmax)
            ax.set_aspect('auto')        
            ax.set_xlabel('decision epoch')
            ax.set_ylabel(name)
            plt.colorbar(action_plot, ax=ax)
            self._action_plots[name] = action_plot
        self._action_back = {name: self._fig.canvas.copy_from_bbox(ax.bbox)
                             for (name, ax) in self._action_ax.items()}
        
        plt.tight_layout()
        plt.show(block=False)
        
    def redraw(self, xticks, losses, actions) -> None:
        
        # draw the loss curve
        self._fig.canvas.restore_region(self._loss_back)
        self._loss_plot.set_xdata(xticks)
        self._loss_plot.set_ydata(losses)        
        self._loss_ax.set_xlim([0, len(xticks)])
        self._loss_ax.set_ylim([np.min(losses), np.max(losses)])
        self._loss_ax.draw_artist(self._loss_plot)
        self._fig.canvas.blit(self._loss_ax.bbox)
        
        # draw the actions
        for (name, values) in actions.items():
            values = np.mean(values, axis=0, dtype=float)
            values = np.reshape(values, newshape=(values.shape[0], -1)).T
            self._fig.canvas.restore_region(self._action_back[name])
            self._action_plots[name].set_array(values)
            self._action_ax[name].draw_artist(self._action_plots[name])
            self._fig.canvas.blit(self._action_ax[name].bbox)
            self._action_plots[name].set_clim([np.min(values), np.max(values)])
        self._fig.canvas.draw()
        self._fig.canvas.flush_events()
        
    def close(self) -> None:
        plt.close(self._fig)
        del self._loss_ax, self._action_ax, \
            self._loss_plot, self._action_plots, self._fig, \
            self._loss_back, self._action_back


class JaxPlannerStatus(Enum):
    NORMAL = 0
    NO_PROGRESS = 1
    PRECONDITION_POSSIBLY_UNSATISFIED = 2
    TIME_BUDGET_REACHED = 3
    ITER_BUDGET_REACHED = 4
    INVALID_GRADIENT = 5
    
    def is_failure(self):
        return self.value >= 3


class JaxBackpropPlanner:
    '''A class for optimizing an action sequence in the given RDDL MDP using 
    gradient descent.'''
    
    def __init__(self, rddl: RDDLLiftedModel,
                 plan: JaxPlan,
                 batch_size_train: int=32,
                 batch_size_test: Optional[int]=None,
                 rollout_horizon: Optional[int]=None,
                 use64bit: bool=False,
                 action_bounds: Optional[Dict[str, Tuple[np.ndarray, np.ndarray]]]=None,
                 optimizer: Callable[..., optax.GradientTransformation]=optax.rmsprop,
                 optimizer_kwargs: Optional[Dict[str, object]]=None,
                 clip_grad: Optional[float]=None,
                 logic: FuzzyLogic=FuzzyLogic(),
                 use_symlog_reward: bool=False,
                 utility: Callable[[jnp.ndarray], float] | str=jnp.mean,
                 utility_kwargs: Optional[Dict[str, object]]=None,
                 cpfs_without_grad: Optional[Set]=None,
                 logger: Optional[Logger]=None) -> None:
        '''Creates a new gradient-based algorithm for optimizing action sequences
        (plan) in the given RDDL. Some operations will be converted to their
        differentiable counterparts; the specific operations can be customized
        by providing a subclass of FuzzyLogic.
        
        :param rddl: the RDDL domain to optimize
        :param plan: the policy/plan representation to optimize
        :param batch_size_train: how many rollouts to perform per optimization 
        step
        :param batch_size_test: how many rollouts to use to test the plan at each
        optimization step
        :param rollout_horizon: lookahead planning horizon: None uses the
        :param use64bit: whether to perform arithmetic in 64 bit
        horizon parameter in the RDDL instance
        :param action_bounds: box constraints on actions
        :param optimizer: a factory for an optax SGD algorithm
        :param optimizer_kwargs: a dictionary of parameters to pass to the SGD
        factory (e.g. which parameters are controllable externally)
        :param clip_grad: maximum magnitude of gradient updates
        :param logic: a subclass of FuzzyLogic for mapping exact mathematical
        operations to their differentiable counterparts 
        :param use_symlog_reward: whether to use the symlog transform on the 
        reward as a form of normalization
        :param utility: how to aggregate return observations to compute utility
        of a policy or plan; must be either a function mapping jax array to a 
        scalar, or a a string identifying the utility function by name 
        ("mean", "mean_var", "entropic", or "cvar" are currently supported)
        :param utility_kwargs: additional keyword arguments to pass hyper-
        parameters to the utility function call
        :param cpfs_without_grad: which CPFs do not have gradients (use straight
        through gradient trick)
        :param logger: to log information about compilation to file
        '''
        self.rddl = rddl
        self.plan = plan
        self.batch_size_train = batch_size_train
        if batch_size_test is None:
            batch_size_test = batch_size_train
        self.batch_size_test = batch_size_test
        if rollout_horizon is None:
            rollout_horizon = rddl.horizon
        self.horizon = rollout_horizon
        if action_bounds is None:
            action_bounds = {}
        self._action_bounds = action_bounds
        self.use64bit = use64bit
        self._optimizer_name = optimizer
        if optimizer_kwargs is None:
            optimizer_kwargs = {'learning_rate': 0.1}
        self._optimizer_kwargs = optimizer_kwargs
        self.clip_grad = clip_grad
        
        # set optimizer
        try:
            optimizer = optax.inject_hyperparams(optimizer)(**optimizer_kwargs)
        except Exception as _:
            raise_warning(
                'Failed to inject hyperparameters into optax optimizer, '
                'rolling back to safer method: please note that modification of '
                'optimizer hyperparameters will not work, and it is '
                'recommended to update your packages and Python distribution.',
                'red')
            optimizer = optimizer(**optimizer_kwargs)     
        if clip_grad is None:
            self.optimizer = optimizer
        else:
            self.optimizer = optax.chain(
                optax.clip(clip_grad),
                optimizer
            )
        
        # set utility
        if isinstance(utility, str):
            utility = utility.lower()
            if utility == 'mean':
                utility_fn = jnp.mean
            elif utility == 'mean_var':
                utility_fn = mean_variance_utility
            elif utility == 'entropic':
                utility_fn = entropic_utility
            elif utility == 'cvar':
                utility_fn = cvar_utility
            else:
                raise RDDLNotImplementedError(
                    f'Utility function <{utility}> is not supported: '
                    'must be one of ["mean", "mean_var", "entropic", "cvar"].')
        else:
            utility_fn = utility
        self.utility = utility_fn
        
        if utility_kwargs is None:
            utility_kwargs = {}
        self.utility_kwargs = utility_kwargs    
        
        self.logic = logic
        self.logic.set_use64bit(self.use64bit)
        self.use_symlog_reward = use_symlog_reward
        if cpfs_without_grad is None:
            cpfs_without_grad = set()
        self.cpfs_without_grad = cpfs_without_grad
        self.logger = logger
        
        self._jax_compile_rddl()        
        self._jax_compile_optimizer()
        
    def summarize_hyperparameters(self):
        print(f'objective and relaxations:\n'
              f'    utility_fn      ={self.utility.__name__}\n'
              f'    utility args    ={self.utility_kwargs}\n'
              f'    use_symlog      ={self.use_symlog_reward}\n'
              f'    lookahead       ={self.horizon}\n'
              f'    model relaxation={type(self.logic).__name__}\n'
              f'    action_bounds   ={self._action_bounds}\n'
              f'    cpfs_no_gradient={self.cpfs_without_grad}\n'
              f'optimizer hyper-parameters:\n'
              f'    use_64_bit      ={self.use64bit}\n'
              f'    optimizer       ={self._optimizer_name.__name__}\n'
              f'    optimizer args  ={self._optimizer_kwargs}\n'
              f'    clip_gradient   ={self.clip_grad}\n'
              f'    batch_size_train={self.batch_size_train}\n'
              f'    batch_size_test ={self.batch_size_test}')
        self.plan.summarize_hyperparameters()
        self.logic.summarize_hyperparameters()
        
    # ===========================================================================
    # COMPILATION SUBROUTINES
    # ===========================================================================

    def _jax_compile_rddl(self):
        rddl = self.rddl
        
        # Jax compilation of the differentiable RDDL for training
        self.compiled = JaxRDDLCompilerWithGrad(
            rddl=rddl,
            logic=self.logic,
            logger=self.logger,
            use64bit=self.use64bit,
            cpfs_without_grad=self.cpfs_without_grad)
        self.compiled.compile(log_jax_expr=True, heading='RELAXED MODEL')
        
        # Jax compilation of the exact RDDL for testing
        self.test_compiled = JaxRDDLCompiler(
            rddl=rddl, 
            logger=self.logger,
            use64bit=self.use64bit)
        self.test_compiled.compile(log_jax_expr=True, heading='EXACT MODEL')
        
    def _jax_compile_optimizer(self):
        
        # policy
        self.plan.compile(self.compiled,
                          _bounds=self._action_bounds,
                          horizon=self.horizon)
        self.train_policy = jax.jit(self.plan.train_policy)
        self.test_policy = jax.jit(self.plan.test_policy)
        
        # roll-outs
        train_rollouts = self.compiled.compile_rollouts(
            policy=self.plan.train_policy,
            n_steps=self.horizon,
            n_batch=self.batch_size_train)
        
        test_rollouts = self.test_compiled.compile_rollouts(
            policy=self.plan.test_policy,
            n_steps=self.horizon,
            n_batch=self.batch_size_test)
        self.test_rollouts = jax.jit(test_rollouts)
        
        # initialization
        self.initialize = jax.jit(self._jax_init())
        
        # losses
        train_loss = self._jax_loss(train_rollouts, use_symlog=self.use_symlog_reward)
        self.train_loss = jax.jit(train_loss)
        self.test_loss = jax.jit(self._jax_loss(test_rollouts, use_symlog=False))
        
        # optimization
        self.update = self._jax_update(train_loss)
    
    def _jax_return(self, use_symlog):
        gamma = self.rddl.discount
        
        # apply discounting of future reward and then optional symlog transform
        def _jax_wrapped_returns(rewards):
            if gamma != 1:
                horizon = rewards.shape[1]
                discount = jnp.power(gamma, jnp.arange(horizon))
                rewards = rewards * discount[jnp.newaxis, ...]
            returns = jnp.sum(rewards, axis=1)
            if use_symlog:
                returns = jnp.sign(returns) * jnp.log(1.0 + jnp.abs(returns))
            return returns
        
        return _jax_wrapped_returns
        
    def _jax_loss(self, rollouts, use_symlog=False): 
        utility_fn = self.utility    
        utility_kwargs = self.utility_kwargs 
        _jax_wrapped_returns = self._jax_return(use_symlog)
        
        # the loss is the average cumulative reward across all roll-outs
        def _jax_wrapped_plan_loss(key, policy_params, hyperparams,
                                   subs, model_params):
            log = rollouts(key, policy_params, hyperparams, subs, model_params)
            rewards = log['reward']
            returns = _jax_wrapped_returns(rewards)
            utility = utility_fn(returns, **utility_kwargs)
            loss = -utility
            return loss, log
        
        return _jax_wrapped_plan_loss
    
    def _jax_init(self):
        init = self.plan.initializer
        optimizer = self.optimizer
        
        def _jax_wrapped_init_policy(key, hyperparams, subs):
            policy_params = init(key, hyperparams, subs)
            opt_state = optimizer.init(policy_params)
            return policy_params, opt_state, None
        
        return _jax_wrapped_init_policy
        
    def _jax_update(self, loss):
        optimizer = self.optimizer
        projection = self.plan.projection
        
        # calculate the plan gradient w.r.t. return loss and update optimizer
        # also perform a projection step to satisfy constraints on actions
        def _jax_wrapped_plan_update(key, policy_params, hyperparams,
                                     subs, model_params, opt_state, opt_aux):
            grad_fn = jax.grad(loss, argnums=1, has_aux=True)
            grad, log = grad_fn(key, policy_params, hyperparams, subs, model_params)  
            updates, opt_state = optimizer.update(grad, opt_state) 
            policy_params = optax.apply_updates(policy_params, updates)
            policy_params, converged = projection(policy_params, hyperparams)
            log['grad'] = grad
            log['updates'] = updates
            return policy_params, converged, opt_state, None, log
        
        return jax.jit(_jax_wrapped_plan_update)
            
    def _batched_init_subs(self, subs): 
        rddl = self.rddl
        n_train, n_test = self.batch_size_train, self.batch_size_test
        
        # batched subs
        init_train, init_test = {}, {}
        for (name, value) in subs.items():
            init_value = self.test_compiled.init_values.get(name, None)
            if init_value is None:
                raise RDDLUndefinedVariableError(
                    f'Variable <{name}> in subs argument is not a '
                    f'valid p-variable, must be one of '
                    f'{set(self.test_compiled.init_values.keys())}.')
            value = np.reshape(value, newshape=np.shape(init_value))[np.newaxis, ...]
            train_value = np.repeat(value, repeats=n_train, axis=0)
            train_value = train_value.astype(self.compiled.REAL)
            init_train[name] = train_value
            init_test[name] = np.repeat(value, repeats=n_test, axis=0)
        
        # make sure next-state fluents are also set
        for (state, next_state) in rddl.next_state.items():
            init_train[next_state] = init_train[state]
            init_test[next_state] = init_test[state]
        
        return init_train, init_test
    
    # ===========================================================================
    # OPTIMIZE API
    # ===========================================================================

    def optimize(self, *args, return_callback: bool=False, **kwargs) -> Dict[str, object]:
        ''' Compute an optimal straight-line plan. Returns the parameters
        for the optimized policy.
        
        :param return_callback: whether to return the callback from training
        instead of the parameters
        :param key: JAX PRNG key   
        :param epochs: the maximum number of steps of gradient descent
        :param train_seconds: total time allocated for gradient descent
        :param plot_step: frequency to plot the plan and save result to disk
        :param model_params: optional model-parameters to override default
        :param policy_hyperparams: hyper-parameters for the policy/plan, such as
        weights for sigmoid wrapping boolean actions
        :param subs: dictionary mapping initial state and non-fluents to 
        their values: if None initializes all variables from the RDDL instance
        :param guess: initial policy parameters: if None will use the initializer
        specified in this instance
        :param verbose: not print (0), print summary (1), print progress (2)
        :param test_rolling_window: the test return is averaged on a rolling 
        window of the past test_rolling_window returns when updating the best
        parameters found so far
        :param tqdm_position: position of tqdm progress bar (for multiprocessing)
        '''
        it = self.optimize_generator(*args, **kwargs)
        callback = deque(it, maxlen=1).pop()
        if return_callback:
            return callback
        else:
            return callback['best_params']
    
    def optimize_generator(self, key: random.PRNGKey,
                           epochs: int=999999,
                           train_seconds: float=120.,
                           plot_step: Optional[int]=None,
                           model_params: Optional[Dict[str, object]]=None,
                           policy_hyperparams: Optional[Dict[str, object]]=None,
                           subs: Optional[Dict[str, object]]=None,
                           guess: Optional[Dict[str, object]]=None,
                           verbose: int=2,
                           test_rolling_window: int=10,
                           tqdm_position: Optional[int]=None) -> Generator[Dict[str, object], None, None]:
        '''Returns a generator for computing an optimal straight-line plan. 
        Generator can be iterated over to lazily optimize the plan, yielding
        a dictionary of intermediate computations.
        
        :param key: JAX PRNG key   
        :param epochs: the maximum number of steps of gradient descent
        :param train_seconds: total time allocated for gradient descent
        :param plot_step: frequency to plot the plan and save result to disk
        :param model_params: optional model-parameters to override default
        :param policy_hyperparams: hyper-parameters for the policy/plan, such as
        weights for sigmoid wrapping boolean actions
        :param subs: dictionary mapping initial state and non-fluents to 
        their values: if None initializes all variables from the RDDL instance
        :param guess: initial policy parameters: if None will use the initializer
        specified in this instance
        :param verbose: not print (0), print summary (1), print progress (2)
        :param test_rolling_window: the test return is averaged on a rolling 
        window of the past test_rolling_window returns when updating the best
        parameters found so far
        :param tqdm_position: position of tqdm progress bar (for multiprocessing)
        '''
        verbose = int(verbose)
        start_time = time.time()
        elapsed_outside_loop = 0
        
        # if policy_hyperparams is not provided
        if policy_hyperparams is None:
            raise_warning('policy_hyperparams is not set, setting 1.0 for '
                          'all action-fluents which could be suboptimal.')
            policy_hyperparams = {action: 1.0 
                                  for action in self.rddl.action_fluents}
        
        # if policy_hyperparams is a scalar
        elif isinstance(policy_hyperparams, (int, float, np.number)):
            raise_warning(f'policy_hyperparams is {policy_hyperparams}, '
                          'setting this value for all action-fluents.')
            hyperparam_value = float(policy_hyperparams)
            policy_hyperparams = {action: hyperparam_value
                                  for action in self.rddl.action_fluents}
            
        # print summary of parameters:
        if verbose >= 1:
            print('==============================================\n'
                  'JAX PLANNER PARAMETER SUMMARY\n'
                  '==============================================')
            self.summarize_hyperparameters()
            print(f'optimize() call hyper-parameters:\n'
                  f'    max_iterations     ={epochs}\n'
                  f'    max_seconds        ={train_seconds}\n'
                  f'    model_params       ={model_params}\n'
                  f'    policy_hyper_params={policy_hyperparams}\n'
                  f'    override_subs_dict ={subs is not None}\n'
                  f'    provide_param_guess={guess is not None}\n'
                  f'    test_rolling_window={test_rolling_window}\n' 
                  f'    plot_frequency     ={plot_step}\n'
                  f'    verbose            ={verbose}\n')
            if verbose >= 2:
                print('EXPRESSION RELAXATION SUMMARY:')
                print(self.compiled.summarize_model_relaxations())
            
        # compute a batched version of the initial values
        if subs is None:
            subs = self.test_compiled.init_values
        else:
            # if some p-variables are not provided, add their default values
            subs = subs.copy()
            added_pvars_to_subs = []
            for (var, value) in self.test_compiled.init_values.items():
                if var not in subs:
                    subs[var] = value
                    added_pvars_to_subs.append(var)
            if added_pvars_to_subs:
                raise_warning(f'p-variables {added_pvars_to_subs} not in '
                              'provided subs, using their initial values '
                              'from the RDDL files.')
        train_subs, test_subs = self._batched_init_subs(subs)
        
        # initialize, model parameters
        if model_params is None:
            model_params = self.compiled.model_params
        model_params_test = self.test_compiled.model_params
        
        # initialize policy parameters
        if guess is None:
            key, subkey = random.split(key)
            policy_params, opt_state, opt_aux = self.initialize(
                subkey, policy_hyperparams, train_subs)
        else:
            policy_params = guess
            opt_state = self.optimizer.init(policy_params)
            opt_aux = None
        
        # initialize running statistics
        best_params, best_loss, best_grad = policy_params, jnp.inf, jnp.inf
        last_iter_improve = 0
        rolling_test_loss = RollingMean(test_rolling_window)
        log = {}
        status = JaxPlannerStatus.NORMAL
        
        # initialize plot area
        if plot_step is None or plot_step <= 0 or plt is None:
            plot = None
        else:
            plot = JaxPlannerPlot(self.rddl, self.horizon)
        xticks, loss_values = [], []
        
        # training loop
        iters = range(epochs)
        if verbose >= 2:
            iters = tqdm(iters, total=100, position=tqdm_position)
        
        for it in iters:
            status = JaxPlannerStatus.NORMAL
            
            # update the parameters of the plan
            key, subkey1, subkey2, subkey3 = random.split(key, num=4)
            policy_params, converged, opt_state, opt_aux, train_log = self.update(
                subkey1, policy_params, policy_hyperparams,
                train_subs, model_params, opt_state, opt_aux)
            if not np.all(converged):
                raise_warning(
                    'Projected gradient method for satisfying action concurrency '
                    'constraints reached the iteration limit: plan is possibly '
                    'invalid for the current instance.', 'red')
                status = JaxPlannerStatus.PRECONDITION_POSSIBLY_UNSATISFIED
            
            # evaluate losses
            train_loss, _ = self.train_loss(
                subkey2, policy_params, policy_hyperparams,
                train_subs, model_params)
            test_loss, log = self.test_loss(
                subkey3, policy_params, policy_hyperparams,
                test_subs, model_params_test)
            test_loss = rolling_test_loss.update(test_loss)
            
            # record the best plan so far
            if test_loss < best_loss:
                best_params, best_loss, best_grad = \
                    policy_params, test_loss, train_log['grad']
                last_iter_improve = it
            
            # save the plan figure
            if plot is not None and it % plot_step == 0:
                xticks.append(it // plot_step)
                loss_values.append(test_loss.item())
                action_values = {name: values 
                                 for (name, values) in log['fluents'].items()
                                 if name in self.rddl.action_fluents}
                plot.redraw(xticks, loss_values, action_values)
            
            # if the progress bar is used
            elapsed = time.time() - start_time - elapsed_outside_loop
            if verbose >= 2:
                iters.n = int(100 * min(1, max(elapsed / train_seconds, it / epochs)))
                iters.set_description(
                    f'[{tqdm_position}] {it:6} it / {-train_loss:14.4f} train / '
                    f'{-test_loss:14.4f} test / {-best_loss:14.4f} best')
                        
            # reached computation budget
            if elapsed >= train_seconds:
                status = JaxPlannerStatus.TIME_BUDGET_REACHED
            if it >= epochs - 1:
                status = JaxPlannerStatus.ITER_BUDGET_REACHED
            
            # numerical error
            if not np.isfinite(train_loss):
                raise_warning(
                    f'Aborting JAX planner due to invalid train loss {train_loss}.',
                    'red')
                status = JaxPlannerStatus.INVALID_GRADIENT
            
            # no progress
            grad_norm_zero, _ = jax.tree_util.tree_flatten(
                jax.tree_map(lambda x: np.allclose(x, 0), train_log['grad']))
            if np.all(grad_norm_zero):
                status = JaxPlannerStatus.NO_PROGRESS
            
            # return a callback
            start_time_outside = time.time()
            yield {
                'status': status,
                'iteration': it,
                'train_return':-train_loss,
                'test_return':-test_loss,
                'best_return':-best_loss,
                'params': policy_params,
                'best_params': best_params,
                'last_iteration_improved': last_iter_improve,
                'grad': train_log['grad'],
                'best_grad': best_grad,
                'updates': train_log['updates'],
                'elapsed_time': elapsed,
                'key': key,
                **log
            }
            elapsed_outside_loop += (time.time() - start_time_outside)
            
            # abortion check
            if status.is_failure():
                break
                        
        # release resources
        if verbose >= 2:
            iters.close()
        if plot is not None:
            plot.close()
        
        # validate the test return
        if log:
            messages = set()
            for error_code in np.unique(log['error']):
                messages.update(JaxRDDLCompiler.get_error_messages(error_code))
            if messages:
                messages = '\n'.join(messages)
                raise_warning('The JAX compiler encountered the following '
                              'problems in the original RDDL '
                              f'during test evaluation:\n{messages}', 'red')                               
        
        # summarize and test for convergence
        if verbose >= 1:
            grad_norm = jax.tree_map(lambda x: np.linalg.norm(x).item(), best_grad)
            diagnosis = self._perform_diagnosis(
                last_iter_improve, -train_loss, -test_loss, -best_loss, grad_norm)
            print(f'summary of optimization:\n'
                  f'    status_code   ={status}\n'
                  f'    time_elapsed  ={elapsed}\n'
                  f'    iterations    ={it}\n'
                  f'    best_objective={-best_loss}\n'
                  f'    best_grad_norm={grad_norm}\n'
                  f'diagnosis: {diagnosis}\n')
    
    def _perform_diagnosis(self, last_iter_improve, 
                           train_return, test_return, best_return, grad_norm):
        max_grad_norm = max(jax.tree_util.tree_leaves(grad_norm))
        grad_is_zero = np.allclose(max_grad_norm, 0)
        
        validation_error = 100 * abs(test_return - train_return) / \
                            max(abs(train_return), abs(test_return))
        
        # divergence if the solution is not finite
        if not np.isfinite(train_return):
            return termcolor.colored('[FAILURE] training loss diverged.', 'red')
            
        # hit a plateau is likely IF:
        # 1. planner does not improve at all
        # 2. the gradient norm at the best solution is zero
        if last_iter_improve <= 1:
            if grad_is_zero:
                return termcolor.colored(
                    '[FAILURE] no progress was made, '
                    f'and max grad norm = {max_grad_norm}, '
                    'likely stuck in a plateau.', 'red')
            else:
                return termcolor.colored(
                    '[FAILURE] no progress was made, '
                    f'but max grad norm = {max_grad_norm} > 0, '
                    'likely due to bad l.r. or other hyper-parameter.', 'red')
        
        # model is likely poor IF:
        # 1. the train and test return disagree
        if not (validation_error < 20):
            return termcolor.colored(
                '[WARNING] progress was made, '
                f'but relative train test error = {validation_error} is high, '
                'likely poor model relaxation around the solution, '
                'or the batch size is too small.', 'yellow')
        
        # model likely did not converge IF:
        # 1. the max grad relative to the return is high
        if not grad_is_zero:
            return_to_grad_norm = abs(best_return) / max_grad_norm
            if not (return_to_grad_norm > 1):
                return termcolor.colored(
                    '[WARNING] progress was made, '
                    f'but max grad norm = {max_grad_norm} is high, '
                    'likely indicates the solution is not locally optimal, '
                    'or the model is not smooth around the solution, '
                    'or the batch size is too small.', 'yellow')
        
        # likely successful
        return termcolor.colored(
            '[SUCCESS] planner appears to have converged successfully '
            '(note: not all potential problems can be ruled out).', 'green')
        
    def get_action(self, key: random.PRNGKey,
                   params: Dict,
                   step: int,
                   subs: Dict,
                   policy_hyperparams: Optional[Dict[str, object]]=None) -> Dict[str, object]:
        '''Returns an action dictionary from the policy or plan with the given
        parameters.
        
        :param key: the JAX PRNG key
        :param params: the trainable parameter PyTree of the policy
        :param step: the time step at which decision is made
        :param subs: the dict of pvariables
        :param policy_hyperparams: hyper-parameters for the policy/plan, such as
        weights for sigmoid wrapping boolean actions (optional)
        '''
        
        # check compatibility of the subs dictionary
        for (var, values) in subs.items():
            
            # must not be grounded
            if RDDLPlanningModel.FLUENT_SEP in var \
            or RDDLPlanningModel.OBJECT_SEP in var:
                raise ValueError(f'State dictionary passed to the JAX policy is '
                                 f'grounded, since it contains the key <{var}>, '
                                 f'but a vectorized environment is required: '
                                 f'please make sure vectorized=True in the RDDLEnv.')
            
            # must be numeric array
            # exception is for POMDPs at 1st epoch when observ-fluents are None
            if not jnp.issubdtype(values.dtype, jnp.number) \
            and not jnp.issubdtype(values.dtype, jnp.bool_):
                if step == 0 and var in self.rddl.observ_fluents:
                    subs[var] = self.test_compiled.init_values[var]
                else:
                    raise ValueError(f'Values assigned to pvariable {var} are '
                                     f'non-numeric of type {values.dtype}: {values}.')
            
        # cast device arrays to numpy
        actions = self.test_policy(key, params, policy_hyperparams, step, subs)
        actions = jax.tree_map(np.asarray, actions)
        return actions      
    

class JaxLineSearchPlanner(JaxBackpropPlanner):
    '''A class for optimizing an action sequence in the given RDDL MDP using 
    linear search gradient descent, with the Armijo condition.'''
    
    def __init__(self, *args,
                 optimizer: Callable[..., optax.GradientTransformation]=optax.sgd,
                 optimizer_kwargs: Dict[str, object]={'learning_rate': 1.0},
                 decay: float=0.8,
                 c: float=0.1,
                 step_max: float=1.0,
                 step_min: float=1e-6,
                 **kwargs) -> None:
        '''Creates a new gradient-based algorithm for optimizing action sequences
        (plan) in the given RDDL using line search. All arguments are the
        same as in the parent class, except:
        
        :param decay: reduction factor of learning rate per line search iteration
        :param c: positive coefficient in Armijo condition, should be in (0, 1)
        :param step_max: initial learning rate for line search
        :param step_min: minimum possible learning rate (line search halts)
        '''
        self.decay = decay
        self.c = c
        self.step_max = step_max
        self.step_min = step_min
        super(JaxLineSearchPlanner, self).__init__(
            *args,
            optimizer=optimizer,
            optimizer_kwargs=optimizer_kwargs,
            **kwargs)
        
    def summarize_hyperparameters(self):
        super(JaxLineSearchPlanner, self).summarize_hyperparameters()
        print(f'linesearch hyper-parameters:\n'
              f'    decay   ={self.decay}\n'
              f'    c       ={self.c}\n'
              f'    lr_range=({self.step_min}, {self.step_max})')
    
    def _jax_update(self, loss):
        optimizer = self.optimizer
        projection = self.plan.projection
        decay, c, lrmax, lrmin = self.decay, self.c, self.step_max, self.step_min
        
        # initialize the line search routine
        @jax.jit
        def _jax_wrapped_line_search_init(key, policy_params, hyperparams,
                                          subs, model_params):
            (f, log), grad = jax.value_and_grad(loss, argnums=1, has_aux=True)(
                key, policy_params, hyperparams, subs, model_params)     
            gnorm2 = jax.tree_map(lambda x: jnp.sum(jnp.square(x)), grad)
            gnorm2 = jax.tree_util.tree_reduce(jnp.add, gnorm2)
            log['grad'] = grad
            return (f, grad, gnorm2, log)
            
        # compute the next trial solution
        @jax.jit
        def _jax_wrapped_line_search_trial(
                step, grad, key, params, hparams, subs, mparams, state):
            state.hyperparams['learning_rate'] = step
            updates, new_state = optimizer.update(grad, state)
            new_params = optax.apply_updates(params, updates)
            new_params, _ = projection(new_params, hparams)
            f_step, _ = loss(key, new_params, hparams, subs, mparams)
            return (f_step, new_params, new_state)
        
        # main iteration of line search     
        def _jax_wrapped_plan_update(key, policy_params, hyperparams,
                                     subs, model_params, opt_state, opt_aux):
            
            # initialize the line search
            f, grad, gnorm2, log = _jax_wrapped_line_search_init(
                key, policy_params, hyperparams, subs, model_params)            
            
            # continue to reduce the learning rate until the Armijo condition holds
            trials = 0
            step = lrmax / decay
            f_step = np.inf
            best_f, best_step, best_params, best_state = np.inf, None, None, None
            while f_step > f - c * step * gnorm2 and step * decay >= lrmin:
                trials += 1
                step *= decay
                f_step, new_params, new_state = _jax_wrapped_line_search_trial(
                    step, grad, key, policy_params, hyperparams, subs, 
                    model_params, opt_state)
                if f_step < best_f:
                    best_f, best_step, best_params, best_state = \
                        f_step, step, new_params, new_state
            
            log['updates'] = None
            log['line_search_iters'] = trials
            log['learning_rate'] = best_step
            return best_params, True, best_state, best_step, log
            
        return _jax_wrapped_plan_update


# ***********************************************************************
# ALL VERSIONS OF RISK FUNCTIONS
# 
# Based on the original paper "A Distributional Framework for Risk-Sensitive 
# End-to-End Planning in Continuous MDPs" by Patton et al., AAAI 2022.
#
# Original risk functions:
# - entropic utility
# - mean-variance approximation
# - conditional value at risk with straight-through gradient trick
#
# ***********************************************************************


@jax.jit
def entropic_utility(returns: jnp.ndarray, beta: float) -> float:
    return (-1.0 / beta) * jax.scipy.special.logsumexp(
            -beta * returns, b=1.0 / returns.size)


@jax.jit
def mean_variance_utility(returns: jnp.ndarray, beta: float) -> float:
    return jnp.mean(returns) - (beta / 2.0) * jnp.var(returns)
    

@jax.jit
def cvar_utility(returns: jnp.ndarray, alpha: float) -> float:
    alpha_mask = jax.lax.stop_gradient(
        returns <= jnp.percentile(returns, q=100 * alpha))
    return jnp.sum(returns * alpha_mask) / jnp.sum(alpha_mask)
    

# ***********************************************************************
# ALL VERSIONS OF CONTROLLERS
# 
# - offline controller is the straight-line planner
# - online controller is the replanning mode
#
# ***********************************************************************

class JaxOfflineController(BaseAgent):
    '''A container class for a Jax policy trained offline.'''
    
    use_tensor_obs = True
    
    def __init__(self, planner: JaxBackpropPlanner, 
                 key: random.PRNGKey,
                 eval_hyperparams: Optional[Dict[str, object]]=None,
                 params: Optional[Dict[str, object]]=None,
                 train_on_reset: bool=False,
                 **train_kwargs) -> None:
        '''Creates a new JAX offline control policy that is trained once, then
        deployed later.
        
        :param planner: underlying planning algorithm for optimizing actions
        :param key: the RNG key to seed randomness
        :param eval_hyperparams: policy hyperparameters to apply for evaluation
        or whenever sample_action is called
        :param params: use the specified policy parameters instead of calling
        planner.optimize()
        :param train_on_reset: retrain policy parameters on every episode reset
        :param **train_kwargs: any keyword arguments to be passed to the planner
        for optimization
        '''
        self.planner = planner
        self.key = key
        self.eval_hyperparams = eval_hyperparams
        self.train_on_reset = train_on_reset
        self.train_kwargs = train_kwargs        
        self.params_given = params is not None
        
        self.step = 0
        if not self.train_on_reset and not self.params_given:
            params = self.planner.optimize(key=self.key, **self.train_kwargs) 
        self.params = params  
        
    def sample_action(self, state):
        self.key, subkey = random.split(self.key)
        actions = self.planner.get_action(
            subkey, self.params, self.step, state, self.eval_hyperparams)
        self.step += 1
        return actions
        
    def reset(self):
        self.step = 0
        if self.train_on_reset and not self.params_given:
            self.params = self.planner.optimize(key=self.key, **self.train_kwargs)


class JaxOnlineController(BaseAgent):
    '''A container class for a Jax controller continuously updated using state 
    feedback.'''
    
    use_tensor_obs = True
    
    def __init__(self, planner: JaxBackpropPlanner, 
                 key: random.PRNGKey,
                 eval_hyperparams: Optional[Dict[str, object]]=None,
                 warm_start: bool=True,
                 **train_kwargs) -> None:
        '''Creates a new JAX control policy that is trained online in a closed-
        loop fashion.
        
        :param planner: underlying planning algorithm for optimizing actions
        :param key: the RNG key to seed randomness
        :param eval_hyperparams: policy hyperparameters to apply for evaluation
        or whenever sample_action is called
        :param warm_start: whether to use the previous decision epoch final
        policy parameters to warm the next decision epoch
        :param **train_kwargs: any keyword arguments to be passed to the planner
        for optimization
        '''
        self.planner = planner
        self.key = key
        self.eval_hyperparams = eval_hyperparams
        self.warm_start = warm_start
        self.train_kwargs = train_kwargs
        self.reset()
     
    def sample_action(self, state):
        planner = self.planner
        params = planner.optimize(
            key=self.key,
            guess=self.guess,
            subs=state,
            **self.train_kwargs)
        self.key, subkey = random.split(self.key)
        actions = planner.get_action(subkey, params, 0, state, self.eval_hyperparams)
        if self.warm_start:
            self.guess = planner.plan.guess_next_epoch(params)
        return actions
        
    def reset(self):
        self.guess = None
    
