import jax
import jaxlib
import jax.numpy as np
from jax import lax
from jax import tree_multimap
import numpy as onp
import collections
from functools import partial
from jax.config import config
config.update("jax_enable_x64", True)

#local imports 
from . import bdf_util
from ..jax_utils import register_pytree_namedtuple

#Adapted from tensorflow_probabilty at https://github.com/tensorflow/probability/blob/master/tensorflow_probability/python/math/ode/bdf.py
#Accessed 2020-06-26

class BDF(object):
    
    def __init__(
                 self, 
                 rtol=1e-3,
                 atol=1e-6,
                 first_step_size=None,
                 safety_factor=0.9,
                 min_step_size_factor=0.1,
                 max_step_size_factor=10.,
                 max_num_steps=np.inf,
                 max_order=bdf_util.MAX_ORDER,
                 max_num_newton_iters=4,
                 newton_tol_factor=0.1,
                 newton_step_size_factor=0.5,
                 bdf_coefficients=[0.,0.1850, -1. / 9., -0.0823, -0.0415, 0.],
                 evaluate_jacobian_lazily=False
                 ):
        self.rtol                 = rtol
        self.atol                 = atol
        self.first_step_size      = first_step_size
        self.safety_factor        = safety_factor
        self.min_step_size_factor = min_step_size_factor
        self.max_step_size_factor = max_step_size_factor
        self.max_num_steps        = max_num_steps
        self.max_order            = max_order
        self.max_num_newton_iters = max_num_newton_iters
        self.newton_tol_factor    = newton_tol_factor
        self.newton_step_size_factor = newton_step_size_factor
        self.bdf_coefficients = bdf_coefficients
        self._evaluate_jacobian_lazily = evaluate_jacobian_lazily
    
    def _solve(self,
               ode_fn,
               initial_time,
               initial_state,
               solution_times,
               jacobian_fn):
    
        def advance_to_solution_time(_states):
            """Takes multiple steps to advance time to `solution_times[n]`."""
            n, diagnostics, iterand, solver_internal_state, state_vec, times = _states
            def step_cond(_states):
                next_time, diagnostics, iterand, *_ = _states
                return (iterand.time < next_time) & (np.equal(diagnostics.status, 0))

            nth_solution_time = solution_times[n]

            [
            _, diagnostics, iterand, solver_internal_state, state_vec,
            times
            ] = jax.lax.while_loop(step_cond, step, [
            nth_solution_time, diagnostics, iterand, solver_internal_state,
            state_vec, times
            ])

            state_vec = jax.ops.index_update(state_vec, 
                                             jax.ops.index[n],
                                             solver_internal_state.backward_differences[0])
            times = jax.ops.index_update(times, 
                                         jax.ops.index[n],
                                         nth_solution_time)

            return (n + 1, diagnostics, iterand, solver_internal_state,
                  state_vec, times)
    
        def step(_states):
            """Takes a single step."""
            next_time, diagnostics, iterand, solver_internal_state, state_vec, times = _states
            distance_to_next_time = next_time - iterand.time
            overstepped = iterand.new_step_size > distance_to_next_time
            iterand = iterand._replace(
            new_step_size=np.where(overstepped, distance_to_next_time,
                                    iterand.new_step_size),
            should_update_step_size=overstepped | iterand.should_update_step_size)
            #lazy jacobian evaluation ?
            #operand = (diagnostics, iterand, solver_internal_state)

            #def true_fn(operand):
                #return operand
            
            #def false_fn(operand):
            #if not self._evaluate_jacobian_lazily:
                #diagnostics, iterand, solver_internal_state = operand
            
            jacobian_is_up_to_date = iterand.jacobian_is_up_to_date
            jacobian_mat = tree_multimap(partial(np.where, self._evaluate_jacobian_lazily and jacobian_is_up_to_date), 
                           iterand.jacobian_mat, 
                           jacobian_fn(iterand.time, solver_internal_state.backward_differences[0]))
            
            num_jacobian_evaluations = tree_multimap(partial(np.where, self._evaluate_jacobian_lazily and jacobian_is_up_to_date), 
                           diagnostics.num_jacobian_evaluations, 
                           diagnostics.num_jacobian_evaluations+1)
            
            diagnostics = diagnostics._replace(
            num_jacobian_evaluations=num_jacobian_evaluations)
            iterand = iterand._replace(
            jacobian_mat=jacobian_mat,
            jacobian_is_up_to_date=jacobian_is_up_to_date)

            #diagnostics, iterand, solver_internal_state = operand 
            #return operand
            
            #diagnostics, iterand, solver_internal_state =  lax.cond(self._evaluate_jacobian_lazily, operand, true_fn, operand, false_fn)

            def maybe_step_cond(_states):
                accepted, diagnostics, *_ = _states
                return np.logical_not(accepted) & np.equal(diagnostics.status, 0)
            _, diagnostics, iterand, solver_internal_state = jax.lax.while_loop(maybe_step_cond, 
                                                                                maybe_step,
                                                                                (False, 
                                                                                    diagnostics, 
                                                                                    iterand, 
                                                                                    solver_internal_state))
            return [next_time, diagnostics, iterand, solver_internal_state,
                state_vec, times]
    
        def maybe_step(_states):
            """Takes a single step only if the outcome has a low enough error."""
            accepted, diagnostics, iterand, solver_internal_state = _states 
            [
            num_jacobian_evaluations, num_matrix_factorizations,
            num_ode_fn_evaluations, status
                ] = diagnostics
            [
            jacobian_mat, jacobian_is_up_to_date, new_step_size, num_steps,
            num_steps_same_size, should_update_jacobian, should_update_step_size,
            time, unitary, upper
            ] = iterand
            [backward_differences, order, step_size] = solver_internal_state
            status = np.where(np.equal(num_steps, self.max_num_steps), -1, 0)
            backward_differences = np.where(should_update_step_size,
                                            bdf_util.interpolate_backward_differences(backward_differences, 
                                                                                        order,
                                                                                        new_step_size / step_size),
                                                    backward_differences)
            step_size = np.where(should_update_step_size, new_step_size, step_size)
            should_update_factorization = should_update_step_size  #pylint: disable=unused-variable
            num_steps_same_size = np.where(should_update_step_size, 0,
                                        num_steps_same_size)

            def update_factorization():
                return bdf_util.newton_qr(jacobian_mat,
                                    self.e.newton_coefficients[order],
                                    step_size)
            #lazy jacobian evaluation?
            unitary, upper = update_factorization()
            num_matrix_factorizations += 1

            tol = self.p.atol + self.p.rtol * np.abs(backward_differences[0])
            newton_tol = self.newton_tol_factor * np.linalg.norm(tol)

            [
            newton_converged, next_backward_difference, next_state_vec,
            newton_num_iters
            ] = bdf_util.newton(backward_differences, 
                                self.max_num_newton_iters,
                                self.e.newton_coefficients[order], 
                                self.p.ode_fn_vec,
                                order,
                                step_size,
                                time,
                                newton_tol,
                                unitary,
                                upper)
            num_steps += 1
            num_ode_fn_evaluations += newton_num_iters

            # If Newton's method failed and the Jacobian was up to date, decrease the
            # step size.
            newton_failed = np.logical_not(newton_converged)
            should_update_step_size = newton_failed & jacobian_is_up_to_date
            new_step_size = step_size * np.where(should_update_step_size,
                                            self.newton_step_size_factor, 1.)

            # If Newton's method failed and the Jacobian was NOT up to date, update
            # the Jacobian.
            should_update_jacobian = newton_failed & np.logical_not(
                                        jacobian_is_up_to_date)

            error_ratio = np.where(newton_converged,
                                    bdf_util.error_ratio(next_backward_difference,
                                    self.e.error_coefficients[order],
                                                            tol),
                                    np.nan)
            accepted = error_ratio < 1.
            converged_and_rejected = newton_converged & np.logical_not(accepted)

            # If Newton's method converged but the solution was NOT accepted, decrease
            # the step size.
            new_step_size = np.where(converged_and_rejected,
                                        bdf_util.next_step_size(step_size,
                                                            order,
                                                            error_ratio,
                                                            self.p.safety_factor,
                                                            self.p.min_step_size_factor,
                                                            self.p.max_step_size_factor),
                                        new_step_size)
            should_update_step_size = should_update_step_size | converged_and_rejected

            # If Newton's method converged and the solution was accepted, update the
            # matrix of backward differences.
            time = np.where(accepted, time + step_size, time)
            backward_differences = np.where(accepted,
                                            bdf_util.update_backward_differences(backward_differences,
                                                next_backward_difference,
                                                next_state_vec, order),
                                                backward_differences)
            jacobian_is_up_to_date = jacobian_is_up_to_date & np.logical_not(accepted)
            num_steps_same_size = np.where(accepted, 
                                            num_steps_same_size + 1,
                                            num_steps_same_size)

            # Order and step size are only updated if we have taken strictly more than
            # order + 1 steps of the same size. This is to prevent the order from
            # being throttled.
            should_update_order_and_step_size = accepted & (
                                                num_steps_same_size > order + 1)
            new_order = order
            new_error_ratio = error_ratio
            for offset in [-1, +1]:
                proposed_order = np.clip(order + offset, 1, self.max_order)
                proposed_error_ratio = bdf_util.error_ratio(
                                                            backward_differences[proposed_order + 1],
                                                            self.e.error_coefficients[proposed_order], tol)
                proposed_error_ratio_is_lower = proposed_error_ratio < new_error_ratio
                new_order = np.where(should_update_order_and_step_size & proposed_error_ratio_is_lower,
                                        proposed_order, 
                                        new_order)
                new_error_ratio = np.where(should_update_order_and_step_size & proposed_error_ratio_is_lower,
                                            proposed_error_ratio,
                                            new_error_ratio)
            order = new_order
            error_ratio = new_error_ratio

            new_step_size = np.where(should_update_order_and_step_size,
                                    bdf_util.next_step_size(step_size, order, error_ratio, self.p.safety_factor,
                                    self.p.min_step_size_factor, self.p.max_step_size_factor),
                                    new_step_size)
            should_update_step_size = (should_update_step_size | should_update_order_and_step_size)

            diagnostics = _BDFDiagnostics(num_jacobian_evaluations,
                                            num_matrix_factorizations,
                                            num_ode_fn_evaluations, status)

            iterand = _BDFIterand(jacobian_mat,
                                    jacobian_is_up_to_date,
                                    new_step_size,
                                    num_steps,
                                    num_steps_same_size,
                                    should_update_jacobian,
                                    should_update_step_size,
                                    time,
                                    unitary,
                                    upper)

            solver_internal_state = _BDFSolverInternalState(backward_differences,
                                                            order,
                                                            step_size)
            return accepted, diagnostics, iterand, solver_internal_state
        
        
        solver_internal_state = self._initialize_solver_internal_state(ode_fn,
                                                                      initial_time,
                                                                      initial_state)
        
        
        diagnostics = _BDFDiagnostics(
          num_jacobian_evaluations=0,
          num_matrix_factorizations=0,
          num_ode_fn_evaluations=0,
          status=0)

        iterand = _BDFIterand(
          jacobian_mat=np.zeros([self.p.num_odes, self.p.num_odes]),
          jacobian_is_up_to_date=False,
          new_step_size=solver_internal_state.step_size,
          num_steps=0,
          num_steps_same_size=0,
          should_update_jacobian=True,
          should_update_step_size=False,
          time=self.p.initial_time,
          unitary=np.zeros([self.p.num_odes, self.p.num_odes]),
          upper=np.zeros([self.p.num_odes, self.p.num_odes]),
        )
        
        num_solution_times = np.shape(solution_times)[0]
        state_vec_size = np.shape(initial_state)[0]
        state_vec = np.zeros([num_solution_times, state_vec_size],dtype=np.float64)
        times = np.zeros([num_solution_times],dtype=np.float64)
        
        def advance_to_solution_time_cond(_states):
            n, diagnostics, *_ = _states
            return (n < num_solution_times) & (np.equal(diagnostics.status, 0))

        [
            _, diagnostics, iterand, solver_internal_state, state_vec,
            times
        ] = jax.lax.while_loop(advance_to_solution_time_cond,
                                advance_to_solution_time, (
                                0, diagnostics, iterand, solver_internal_state,
                                state_vec, times
                            ))
        return Results(times=times,
                      states=state_vec,
                      diagnostics=diagnostics,
                      solver_internal_state=solver_internal_state)
    
        
    def _get_coefficients(self,bdf_coefficients):
        newton_coefficients = 1. / (
            (1. - bdf_coefficients) * bdf_util.RECIPROCAL_SUMS)
        
        error_coefficients = bdf_coefficients * bdf_util.RECIPROCAL_SUMS + 1. / (
            bdf_util.ORDERS + 1)
        
        return newton_coefficients, error_coefficients
    
    def _get_common_params_and_coefficients(self,
                                            ode_fn,
                                            initial_time,
                                            initial_state):
        #krishna: find jaxy way of flatten
        atol, rtol = np.array(self.atol,dtype=np.float64), np.array(self.rtol, dtype=np.float64)
        min_step_size_factor, max_step_size_factor = np.array(self.min_step_size_factor,dtype=np.float64), np.array(self.max_step_size_factor, dtype=np.float64)
        max_order, max_num_newton_iters = np.array(self.max_order, dtype=np.int64), np.array(self.max_num_newton_iters,dtype=np.int64)
        max_num_steps = np.array(self.max_num_steps,dtype=np.int64)
        newton_tol_factor, newton_step_size_factor = np.array(self.newton_tol_factor,dtype=np.float64), np.array(self.newton_step_size_factor, dtype=np.float64)
        safety_factor = np.array(self.safety_factor, dtype=np.float64)
        bdf_coefficients = np.array(self.bdf_coefficients,dtype=np.float64)
        initial_state_vec = initial_state.flatten()
        ode_fn_vec = bdf_util.get_ode_fn_vec(ode_fn,initial_time,initial_state)
        num_odes = np.shape(initial_state_vec)[0]

        newton_coefficients, error_cofficients = self._get_coefficients(bdf_coefficients)

        _params = SolverParams(atol=atol,
                             rtol=rtol,
                             min_step_size_factor=min_step_size_factor,
                             max_step_size_factor=max_step_size_factor,
                             safety_factor=safety_factor,
                             max_num_steps=max_num_steps,
                             max_order=max_order,
                             max_num_newton_iters=max_num_newton_iters,
                             newton_tol_factor=newton_tol_factor,
                             newton_step_size_factor=newton_step_size_factor,
                             bdf_coefficients=bdf_coefficients,
                             initial_state_vec=initial_state_vec,
                             ode_fn_vec=ode_fn_vec,
                             initial_time=initial_time,
                             num_odes=num_odes)
        _coefficients = Coefficients(newton_coefficients=newton_coefficients,
                                    error_coefficients=error_cofficients)
        return _params,_coefficients
    
    def _initialize_solver_internal_state(self,
                                          ode_fn,
                                          initial_time,
                                          initial_state):
    
        self.p, self.e = self._get_common_params_and_coefficients( 
                                            ode_fn,
                                            initial_time,
                                            initial_state)

        first_step_size = bdf_util.first_step_size(atol=self.atol,
                                                  first_order_error_coefficient=self.e.error_coefficients[1],
                                                  initial_state_vec=self.p.initial_state_vec,
                                                  initial_time=initial_time,
                                                  ode_fn_vec=self.p.ode_fn_vec,
                                                  rtol=self.rtol,
                                                  safety_factor=self.safety_factor)
        
        first_order_backward_difference = self.p.ode_fn_vec(initial_time, self.p.initial_state_vec) * first_step_size
        
        backward_differences = np.concatenate([self.p.initial_state_vec[np.newaxis,:],
                                           first_order_backward_difference[np.newaxis,:],
                                           np.zeros(np.array(np.stack((bdf_util.MAX_ORDER+1,self.p.num_odes)),dtype=np.int64))],
                                           axis=0)
        return _BDFSolverInternalState(
            backward_differences=backward_differences,
            order=1,
            step_size=first_step_size)

    @partial(jax.jit, static_argnums=(0, 1, 5))
    def solve(self, 
              ode_fn,
              initial_time,
              initial_state,
              solution_times,
              jacobian_fn):
    
        results = self._solve(
                              ode_fn=ode_fn,
                              initial_time=initial_time,
                              initial_state=initial_state,
                              solution_times=solution_times,
                              jacobian_fn=jacobian_fn)
        return results



class Results(collections.namedtuple("Results",
                                    ["times",
                                     "states",
                                     "diagnostics",
                                     "solver_internal_state"])): #
    """
    namedtuple class to store results from ode solver
    """
    def __new__(cls, times, states, diagnostics, solver_internal_state):
        return super(Results, cls).__new__(
            cls,  times, states, diagnostics, solver_internal_state)

register_pytree_namedtuple(Results) #JAX pytree 


class _BDFDiagnostics(collections.namedtuple('_BDFDiagnostics',[
                                            'num_jacobian_evaluations',
                                            'num_matrix_factorizations',
                                            'num_ode_fn_evaluations',
                                            'status',
                                            ])):
    """
    namedtuple class to store diagnostics
    """
    def __new__(cls, num_jacobian_evaluations, num_matrix_factorizations, 
                     num_ode_fn_evaluations, status):
        return super(_BDFDiagnostics, cls).__new__(
            cls, num_jacobian_evaluations, num_matrix_factorizations, 
                     num_ode_fn_evaluations, status)

register_pytree_namedtuple(_BDFDiagnostics) #JAX pytree 





class _BDFIterand(collections.namedtuple('_BDFIterand',[
                                         'jacobian_mat',
                                         'jacobian_is_up_to_date',
                                         'new_step_size',
                                         'num_steps',
                                         'num_steps_same_size',
                                         'should_update_jacobian',
                                         'should_update_step_size',
                                         'time',
                                         'unitary',
                                         'upper'])):
    """
    namedtuple class to store iterand state
    """
    def __new__(cls, jacobian_mat, jacobian_is_up_to_date, 
                     new_step_size, num_steps, num_steps_same_size,
                     should_update_jacobian, should_update_step_size,
                     time, unitary, upper):
        return super(_BDFIterand, cls).__new__(
            cls, jacobian_mat, jacobian_is_up_to_date, 
                     new_step_size, num_steps, num_steps_same_size,
                     should_update_jacobian, should_update_step_size,
                     time, unitary, upper)

register_pytree_namedtuple(_BDFIterand) #JAX pytree 


class _BDFSolverInternalState(collections.namedtuple('_BDFSolverInternalState', [
                            'backward_differences',
                            'order',
                            'step_size',
                            ])):
    """
    Returned by the solver to warm start future invocations
    """
    def __new__(cls, backward_differences, order, step_size):
        return super(_BDFSolverInternalState, cls).__new__(
            cls, backward_differences, order, step_size)

register_pytree_namedtuple(_BDFSolverInternalState) #JAX pytree 



class SolverParams(
      collections.namedtuple('SolverParams',["rtol",
                             "atol",
                             "safety_factor",
                             "min_step_size_factor",
                             "max_step_size_factor",
                             "max_num_steps",
                             "max_order",
                             "max_num_newton_iters",
                             "newton_tol_factor",
                             "newton_step_size_factor",
                             "bdf_coefficients",
                             "initial_state_vec",
                             "ode_fn_vec",
                             "initial_time",
                             "num_odes"])):
    def __new__(cls, rtol, atol, safety_factor, min_step_size_factor,
                     max_step_size_factor, max_num_steps, max_order, 
                     max_num_newton_iters, newton_tol_factor, 
                     newton_step_size_factor, bdf_coefficients,
                     initial_state_vec, ode_fn_vec, initial_time,
                     num_odes):
        return super(SolverParams, cls).__new__(cls, rtol, atol, safety_factor, min_step_size_factor,
                     max_step_size_factor, max_num_steps, max_order, 
                     max_num_newton_iters, newton_tol_factor, 
                     newton_step_size_factor, bdf_coefficients,
                     initial_state_vec, ode_fn_vec, initial_time,
                     num_odes)

register_pytree_namedtuple(SolverParams)


class Coefficients(
      collections.namedtuple('Coefficients',["newton_coefficients",
                                           "error_coefficients"])):
    def __new__(cls, newton_coefficients, error_coefficients):
        return super(Coefficients, cls).__new__(cls, newton_coefficients, error_coefficients)

register_pytree_namedtuple(Coefficients)