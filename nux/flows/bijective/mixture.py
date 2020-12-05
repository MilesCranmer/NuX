import jax
import jax.numpy as jnp
import nux.util as util
from jax import random, vmap
from functools import partial
import haiku as hk
from typing import Optional, Mapping, Callable, Sequence
from nux.internal.layer import Layer
import nux.util as util
from jax.scipy.special import logsumexp
import nux.networks as net
from nux.util import logistic_cdf_mixture_logit
import nux

__all__ = ["GaussianMixtureCDF",
           "LogisticMixtureCDF",
           "CoupingGaussianMixtureCDF",
           "CoupingLogisticMixtureCDF",
           "LogitsticMixtureLogit",
           "CouplingLogitsticMixtureLogit"]

################################################################################################################

def bisection_body(f, val):
  x, current_x, current_z, lower, upper, dx, i = val

  gt = current_x > x
  lt = 1.0 - gt

  new_z = gt*0.5*(current_z + lower) + lt*0.5*(current_z + upper)
  lower = gt*lower                   + lt*current_z
  upper = gt*current_z               + lt*upper

  current_z = new_z
  current_x = f(current_z)
  dx = current_x - x

  return x, current_x, current_z, lower, upper, dx, i + 1

def bisection(f, lower, upper, x, atol=1e-8, max_iters=10000):
  # Compute f^{-1}(x) using the bisection method.  f must be monotonic.
  z = jnp.zeros_like(x)

  def cond_fun(val):
    x, current_x, current_z, lower, upper, dx, i = val

    max_iters_reached = jnp.where(i > max_iters, True, False)
    tolerance_achieved = jnp.allclose(dx, 0.0, atol=atol)

    return ~(max_iters_reached | tolerance_achieved)

  val = (x, f(z), z, lower, upper, jnp.ones_like(x)*10.0, 0.0)
  val = jax.lax.while_loop(cond_fun, partial(bisection_body, f), val)
  x, current_x, current_z, lower, upper, dx, i = val
  return current_z

################################################################################################################

def mixture_forward(f_and_log_det_fun, x, theta, needs_vmap=True, with_affine_coupling=True):
  # Split the parameters
  n_components = (theta.shape[-1] - 2)//3
  weight_logits, means, log_scales, log_s, t  = jnp.split(theta, jnp.array([n_components,
                                                                            2*n_components,
                                                                            3*n_components,
                                                                            3*n_components + 1]), axis=-1)
  if with_affine_coupling:
    log_s = 1.5*jnp.tanh(log_s[...,0])
    t = t[...,0]

  # We are going to vmap over each pixel
  f_and_log_det = f_and_log_det_fun
  if needs_vmap:
    for i in range(len(x.shape)):
      in_axes = [0, 0, 0, 0]
      in_axes[0] = None if weight_logits.ndim - 1 <= i else 0
      in_axes[1] = None if means.ndim - 1 <= i else 0
      in_axes[2] = None if log_scales.ndim - 1 <= i else 0
      f_and_log_det = vmap(f_and_log_det, in_axes=in_axes)

  # Apply the mixture
  z, log_det = f_and_log_det(weight_logits, means, log_scales, x)
  log_det = log_det.sum()

  # Apply the elementwise shift/scale
  if with_affine_coupling:
    z = (z - t)*jnp.exp(-log_s)
    log_det += -log_s.sum()#*util.list_prod(x.shape)

  return z, log_det

def mixture_inverse(eval_fun, log_det_fun, x, theta, needs_vmap=True, with_affine_coupling=True):
  # Split the parameters
  n_components = (theta.shape[-1] - 2)//3
  weight_logits, means, log_scales, log_s, t  = jnp.split(theta, jnp.array([n_components,
                                                                            2*n_components,
                                                                            3*n_components,
                                                                            3*n_components + 1]), axis=-1)
  if with_affine_coupling:
    log_s = 1.5*jnp.tanh(log_s[...,0])
    t = t[...,0]

    # Undo the elementwise shift/scale
    x = x*jnp.exp(log_s) + t
    elementwise_log_det = -log_s.sum()
  else:
    elementwise_log_det = 0.0

  def bisection_no_vmap(weight_logits, means, log_scales, x):
    # Write a wrapper around the inverse function
    # assert weight_logits.ndim == 1
    # assert x.ndim == 0

    # If we're outside of this range, then there's a bigger problem in the rest of the network.
    lower = jnp.zeros_like(x) - 1000
    upper = jnp.zeros_like(x) + 1000

    filled_f = partial(eval_fun, weight_logits, means, log_scales)
    return bisection(filled_f, lower, upper, x)

  # We are going to vmap over each pixel
  f_inv = bisection_no_vmap
  log_det = log_det_fun

  if needs_vmap:
    for i in range(len(x.shape)):
      in_axes = [0, 0, 0, 0]
      in_axes[0] = None if weight_logits.ndim - 1 <= i else 0
      in_axes[1] = None if means.ndim - 1 <= i else 0
      in_axes[2] = None if log_scales.ndim - 1 <= i else 0
      f_inv = vmap(f_inv, in_axes=in_axes)
      log_det = vmap(log_det, in_axes=in_axes)

  # Apply the mixture inverse
  z = f_inv(weight_logits, means, log_scales, x)
  return z, log_det(weight_logits, means, log_scales, z).sum() + elementwise_log_det

################################################################################################################

class MixtureCDF(Layer):

  def __init__(self,
               n_components: int=4,
               name: str="mixture_cdf",
               **kwargs
  ):
    """ Base class for a mixture cdf with no coupling
    Args:
      n_components: Number of mixture components to use
      name        : Optional name for this module.
    """
    super().__init__(name=name, **kwargs)
    self.n_components = n_components

    self.forward = partial(mixture_forward, self.f_and_log_det)
    self.inverse = partial(mixture_inverse, self.f, self.log_det)

  def f(self, weight_logits, means, log_scales, x):
    assert 0

  def log_det(self, weight_logits, means, log_scales, x):
    assert 0

  def f_and_log_det(self, weight_logits, means, log_scales, x):
    return self.f(weight_logits, means, log_scales, x), self.log_det(weight_logits, means, log_scales, x)

  def call(self,
           inputs: Mapping[str, jnp.ndarray],
           rng: jnp.ndarray=None,
           sample: Optional[bool]=False,
           **kwargs
  ) -> Mapping[str, jnp.ndarray]:
    x = inputs["x"]
    outputs = {}

    x_shape = self.get_unbatched_shapes(sample)["x"]
    theta = hk.get_parameter("theta", shape=x_shape + (3*self.n_components + 2,), dtype=x.dtype, init=hk.initializers.RandomNormal(0.1))
    if sample == False:
      z, log_det = self.auto_batch(self.forward, in_axes=(0, None))(x, theta)
    else:
      z, log_det = self.auto_batch(self.inverse, in_axes=(0, None))(x, theta)

    outputs = {"x": z, "log_det": log_det}

    return outputs

################################################################################################################

from nux.flows.bijective.coupling_base import CouplingBase

class CouplingMixtureCDF(CouplingBase):

  def __init__(self,
               n_components: int=8,
               create_network: Callable=None,
               network_kwargs: Optional=None,
               use_condition: bool=False,
               name: str="coupling_mixture_cdf",
               **kwargs
  ):
    """ Base class for a mixture cdf with coupling
    Args:
      n_components  : Number of mixture components to use
      create_network: Function to create the conditioner network.  Should accept a tuple
                      specifying the output shape.  See coupling_base.py
      use_condition : Should we use inputs["condition"] to form t([xb, condition]), s([xb, condition])?
      network_kwargs: Dictionary with settings for the default network (see get_default_network in util.py)
      name          : Optional name for this module.
    """
    super().__init__(create_network=create_network,
                     axis=-1,
                     split_kind="channel",
                     use_condition=use_condition,
                     name=name,
                     network_kwargs=network_kwargs,
                     **kwargs)
    self.n_components = n_components

    self.forward = partial(mixture_forward, self.f_and_log_det)
    self.inverse = partial(mixture_inverse, self.f, self.log_det)

  def f(self, weight_logits, means, log_scales, x):
    assert 0

  def log_det(self, weight_logits, means, log_scales, x):
    assert 0

  def f_and_log_det(self, weight_logits, means, log_scales, x):
    return self.f(weight_logits, means, log_scales, x), self.log_det(weight_logits, means, log_scales, x)

  def get_out_shape(self, x):
    x_shape = x.shape[len(self.batch_shape):]
    out_dim = x_shape[-1]*3*self.n_components
    return x_shape[:-1] + (out_dim,)

  def transform(self, x, params=None, sample=False):
    if params is None:
      x_shape = x.shape[len(self.batch_shape):]
      theta = hk.get_parameter("theta", shape=x_shape + (3*self.n_components + 2,), dtype=x.dtype, init=hk.initializers.RandomNormal(0.1))
      in_axes = (0, None)
    else:
      theta = params.reshape(x.shape + (3*self.n_components,))
      in_axes = (0, 0)

    if sample == False:
      z, log_det = self.auto_batch(self.forward, in_axes=in_axes)(x, theta)
    else:
      z, log_det = self.auto_batch(self.inverse, in_axes=in_axes)(x, theta)

    return z, log_det

################################################################################################################

class _GaussianMixtureMixin():

  def __init__(self,
               n_components: int=4,
               name: str="gaussian_mixture_cdf",
               **kwargs
  ):
    """ Mix in class for Gaussian mixture cdf models
    Args:
      n_components  : Number of mixture components to use
      name          : Optional name for this module.
    """
    super().__init__(n_components=n_components, name=name, **kwargs)

  def f(self, weight_logits, means, log_scales, x):
    weight_logits = jax.nn.log_softmax(weight_logits)
    dx = x - means
    cdf = jax.scipy.special.ndtr(dx*jnp.exp(-0.5*log_scales))
    z = jnp.sum(jnp.exp(weight_logits)*cdf)
    return z

  def log_det(self, weight_logits, means, log_scales, x):
    weight_logits = jax.nn.log_softmax(weight_logits)
    # log_det is log_pdf(x)
    dx = x - means
    log_pdf = -0.5*(dx**2)*jnp.exp(-log_scales) - 0.5*log_scales - 0.5*jnp.log(2*jnp.pi)
    return logsumexp(weight_logits + log_pdf, axis=-1).sum()


class _LogitsticMixtureMixin():

  def __init__(self,
               n_components: int=4,
               name: str="logistic_mixture_cdf",
               **kwargs
  ):
    """ Mix in class for logistic mixture cdf models
    Args:
      n_components  : Number of mixture components to use
      name          : Optional name for this module.
    """
    super().__init__(n_components=n_components, name=name, **kwargs)

  def f(self, weight_logits, means, log_scales, x):
    weight_logits = jax.nn.log_softmax(weight_logits)
    z_scores = (x - means)*jnp.exp(-log_scales)
    log_cdf = jax.nn.log_sigmoid(z_scores)
    z = jax.scipy.special.logsumexp(weight_logits + log_cdf, axis=-1).sum()
    return jnp.exp(z)

  def log_det(self, weight_logits, means, log_scales, x):
    weight_logits = jax.nn.log_softmax(weight_logits)
    # log_det is log_pdf(x)
    z_scores = (x - means)*jnp.exp(-log_scales)
    log_pdf = -log_scales + jax.nn.log_sigmoid(z_scores) + jax.nn.log_sigmoid(-z_scores)
    return logsumexp(weight_logits + log_pdf, axis=-1).sum()


class _LogitsticMixtureLogitMixin():

  def __init__(self,
               n_components: int=4,
               restrict_scales: bool=True,
               name: str="logistic_mixture_cdf_logit",
               **kwargs
  ):
    """ Mix in class for logistic mixture cdf followed by logit models.
        This works pretty well in practice.  See nux/networks/nonlinearities.py
    Args:
      n_components   : Number of mixture components to use
      restrict_scales: Whether or not to bound the scales.  If log_scales is
                       unbounded, we can get model more complex distributions
                       at the risk of numerical instability.
      name           : Optional name for this module.
    """
    super().__init__(n_components=n_components, name=name, **kwargs)
    self.restrict_scales = restrict_scales
    self.forward = partial(self.forward, needs_vmap=False)
    self.inverse = partial(self.inverse, needs_vmap=False)

  def f(self, weight_logits, means, log_scales, x):
    if self.restrict_scales:
      # log_scales = 1.5*jnp.tanh(log_scales)
      log_scales = jnp.maximum(-7.0, log_scales)

    return jax.jit(logistic_cdf_mixture_logit)(weight_logits, means, log_scales, x)

  def log_det(self, weight_logits, means, log_scales, x):
    return self.f_and_log_det(weight_logits, means, log_scales, x)[1]

  def f_and_log_det(self, weight_logits, means, log_scales, x):
    if self.restrict_scales:
      # log_scales = 1.5*jnp.tanh(log_scales)
      log_scales = jnp.maximum(-7.0, log_scales)

    primals = weight_logits, means, log_scales, x
    tangents = jax.tree_map(jnp.zeros_like, primals[:-1]) + (jnp.ones_like(x),)
    z, dzdx = jax.jit(jax.jvp, static_argnums=(0,))(logistic_cdf_mixture_logit, primals, tangents)
    log_det = jnp.log(dzdx)
    return z, log_det

################################################################################################################

class GaussianMixtureCDF(_GaussianMixtureMixin, MixtureCDF):
  pass

class LogisticMixtureCDF(_LogitsticMixtureMixin, MixtureCDF):
  pass

class CoupingGaussianMixtureCDF(_GaussianMixtureMixin, CouplingMixtureCDF):
  pass

class CoupingLogisticMixtureCDF(_LogitsticMixtureMixin, CouplingMixtureCDF):
  pass

class LogitsticMixtureLogit(_LogitsticMixtureLogitMixin, MixtureCDF):
  pass

class CouplingLogitsticMixtureLogit(_LogitsticMixtureLogitMixin, CouplingMixtureCDF):
  pass
