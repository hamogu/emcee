# -*- coding: utf-8 -*-

import warnings

import numpy as np

from .backends import Backend
from .model import Model
from .moves import StretchMove
from .pbar import get_progress_bar
from .state import State
from .utils import deprecated, deprecation_warning

__all__ = ["EnsembleSampler"]

try:
    from collections.abc import Iterable
except ImportError:
    # for py2.7, will be an Exception in 3.8
    from collections import Iterable


class EnsembleSampler(object):
    """An ensemble MCMC sampler

    Args:
        nwalkers (int): The number of walkers in the ensemble.
        ndim (int): Number of dimensions in the parameter space.
        log_prob_fn (callable): A function that takes a vector in the
            parameter space as input and returns the natural logarithm of the
            posterior probability (up to an additive constant) for that
            position.
        moves (Optional): This can be a single move object, a list of moves,
            or a "weighted" list of the form ``[(emcee.moves.StretchMove(),
            0.1), ...]``. When running, the sampler will randomly select a
            move from this list (optionally with weights) for each proposal.
            (default: :class:`StretchMove`)
        args (Optional): A list of extra positional arguments for
            ``log_prob_fn``. ``log_prob_fn`` will be called with the sequence
            ``log_pprob_fn(p, *args, **kwargs)``.
        kwargs (Optional): A dict of extra keyword arguments for
            ``log_prob_fn``. ``log_prob_fn`` will be called with the sequence
            ``log_pprob_fn(p, *args, **kwargs)``.
        pool (Optional): An object with a ``map`` method that follows the same
            calling sequence as the built-in ``map`` function. This is
            generally used to compute the log-probabilities for the ensemble
            in parallel.
        backend (Optional): Either a :class:`backends.Backend` or a subclass
            (like :class:`backends.HDFBackend`) that is used to store and
            serialize the state of the chain. By default, the chain is stored
            as a set of numpy arrays in memory, but new backends can be
            written to support other mediums.
        vectorize (Optional[bool]): If ``True``, ``log_prob_fn`` is expected
            to accept a list of position vectors instead of just one. Note
            that ``pool`` will be ignored if this is ``True``.
            (default: ``False``)

    """

    def __init__(
        self,
        nwalkers,
        ndim,
        log_prob_fn,
        pool=None,
        moves=None,
        args=None,
        kwargs=None,
        backend=None,
        vectorize=False,
        blobs_dtype=None,
        # Deprecated...
        a=None,
        postargs=None,
        threads=None,
        live_dangerously=None,
        runtime_sortingfn=None,
    ):
        # Warn about deprecated arguments
        if a is not None:
            deprecation_warning(
                "The 'a' argument is deprecated, use 'moves' instead"
            )
        if threads is not None:
            deprecation_warning("The 'threads' argument is deprecated")
        if runtime_sortingfn is not None:
            deprecation_warning(
                "The 'runtime_sortingfn' argument is deprecated"
            )
        if live_dangerously is not None:
            deprecation_warning(
                "The 'live_dangerously' argument is deprecated"
            )

        # Parse the move schedule
        if moves is None:
            self._moves = [StretchMove()]
            self._weights = [1.0]
        elif isinstance(moves, Iterable):
            try:
                self._moves, self._weights = zip(*moves)
            except TypeError:
                self._moves = moves
                self._weights = np.ones(len(moves))
        else:
            self._moves = [moves]
            self._weights = [1.0]
        self._weights = np.atleast_1d(self._weights).astype(float)
        self._weights /= np.sum(self._weights)

        self.pool = pool
        self.vectorize = vectorize
        self.blobs_dtype = blobs_dtype

        self.ndim = ndim
        self.nwalkers = nwalkers
        self.backend = Backend() if backend is None else backend

        # Deal with re-used backends
        if not self.backend.initialized:
            self._previous_state = None
            self.reset()
            state = np.random.get_state()
        else:
            # Check the backend shape
            if self.backend.shape != (self.nwalkers, self.ndim):
                raise ValueError(
                    (
                        "the shape of the backend ({0}) is incompatible with the "
                        "shape of the sampler ({1})"
                    ).format(self.backend.shape, (self.nwalkers, self.ndim))
                )

            # Get the last random state
            state = self.backend.random_state
            if state is None:
                state = np.random.get_state()

            # Grab the last step so that we can restart
            it = self.backend.iteration
            if it > 0:
                self._previous_state = self.get_last_sample()

        # This is a random number generator that we can easily set the state
        # of without affecting the numpy-wide generator
        self._random = np.random.mtrand.RandomState()
        self._random.set_state(state)

        # Do a little bit of _magic_ to make the likelihood call with
        # ``args`` and ``kwargs`` pickleable.
        self.log_prob_fn = _FunctionWrapper(log_prob_fn, args, kwargs)

    @property
    def random_state(self):
        """
        The state of the internal random number generator. In practice, it's
        the result of calling ``get_state()`` on a
        ``numpy.random.mtrand.RandomState`` object. You can try to set this
        property but be warned that if you do this and it fails, it will do
        so silently.

        """
        return self._random.get_state()

    @random_state.setter  # NOQA
    def random_state(self, state):
        """
        Try to set the state of the random number generator but fail silently
        if it doesn't work. Don't say I didn't warn you...

        """
        try:
            self._random.set_state(state)
        except:
            pass

    @property
    def iteration(self):
        return self.backend.iteration

    def reset(self):
        """
        Reset the bookkeeping parameters

        """
        self.backend.reset(self.nwalkers, self.ndim)

    def __getstate__(self):
        # In order to be generally picklable, we need to discard the pool
        # object before trying.
        d = self.__dict__
        d["pool"] = None
        return d

    def sample(
        self,
        initial_state,
        log_prob0=None,  # Deprecated
        rstate0=None,  # Deprecated
        blobs0=None,  # Deprecated
        iterations=1,
        tune=False,
        skip_initial_state_check=False,
        thin_by=1,
        thin=None,
        store=True,
        progress=False,
    ):
        """Advance the chain as a generator

        Args:
            initial_state (State or ndarray[nwalkers, ndim]): The initial
                :class:`State` or positions of the walkers in the
                parameter space.
            iterations (Optional[int]): The number of steps to generate.
            tune (Optional[bool]): If ``True``, the parameters of some moves
                will be automatically tuned.
            thin_by (Optional[int]): If you only want to store and yield every
                ``thin_by`` samples in the chain, set ``thin_by`` to an
                integer greater than 1. When this is set, ``iterations *
                thin_by`` proposals will be made.
            store (Optional[bool]): By default, the sampler stores (in memory)
                the positions and log-probabilities of the samples in the
                chain. If you are using another method to store the samples to
                a file or if you don't need to analyze the samples after the
                fact (for burn-in for example) set ``store`` to ``False``.
            progress (Optional[bool or str]): If ``True``, a progress bar will
                be shown as the sampler progresses. If a string, will select a
                specific ``tqdm`` progress bar - most notable is
                ``'notebook'``, which shows a progress bar suitable for
                Jupyter notebooks.  If ``False``, no progress bar will be
                shown.
            skip_initial_state_check (Optional[bool]): If ``True``, a check that
                the initial_state can fully explore the space will be skipped.
                (default: ``False``)


        Every ``thin_by`` steps, this generator yields the
        :class:`State` of the ensemble.

        """
        # Interpret the input as a walker state and check the dimensions.
        state = State(initial_state, copy=True)
        if np.shape(state.coords) != (self.nwalkers, self.ndim):
            raise ValueError("incompatible input dimensions")
        if (not skip_initial_state_check) and np.linalg.cond(
            np.atleast_2d(np.cov(state.coords, rowvar=False))
        ) > 1e8:
            warnings.warn(
                "Initial state is not linearly independent and it will not "
                "allow a full exploration of parameter space",
                category=RuntimeWarning,
            )

        # Try to set the initial value of the random number generator. This
        # fails silently if it doesn't work but that's what we want because
        # we'll just interpret any garbage as letting the generator stay in
        # it's current state.
        if rstate0 is not None:
            deprecation_warning(
                "The 'rstate0' argument is deprecated, use a 'State' "
                "instead"
            )
            state.random_state = rstate0
        self.random_state = state.random_state

        # If the initial log-probabilities were not provided, calculate them
        # now.
        if log_prob0 is not None:
            deprecation_warning(
                "The 'log_prob0' argument is deprecated, use a 'State' "
                "instead"
            )
            state.log_prob = log_prob0
        if blobs0 is not None:
            deprecation_warning(
                "The 'blobs0' argument is deprecated, use a 'State' instead"
            )
            state.blobs = blobs0
        if state.log_prob is None:
            state.log_prob, state.blobs = self.compute_log_prob(state.coords)
        if np.shape(state.log_prob) != (self.nwalkers,):
            raise ValueError("incompatible input dimensions")

        # Check to make sure that the probability function didn't return
        # ``np.nan``.
        if np.any(np.isnan(state.log_prob)):
            raise ValueError("The initial log_prob was NaN")

        # Deal with deprecated thin argument
        if thin is not None:
            deprecation_warning(
                "The 'thin' argument is deprecated. " "Use 'thin_by' instead."
            )

            # Check that the thin keyword is reasonable.
            thin = int(thin)
            if thin <= 0:
                raise ValueError("Invalid thinning argument")

            yield_step = 1
            checkpoint_step = thin
            iterations = int(iterations)
            if store:
                nsaves = iterations // checkpoint_step
                self.backend.grow(nsaves, state.blobs)

        else:
            # Check that the thin keyword is reasonable.
            thin_by = int(thin_by)
            if thin_by <= 0:
                raise ValueError("Invalid thinning argument")

            yield_step = thin_by
            checkpoint_step = thin_by
            iterations = int(iterations)
            if store:
                self.backend.grow(iterations, state.blobs)

        # Set up a wrapper around the relevant model functions
        if self.pool is not None:
            map_fn = self.pool.map
        else:
            map_fn = map
        model = Model(
            self.log_prob_fn, self.compute_log_prob, map_fn, self._random
        )

        # Inject the progress bar
        total = iterations * yield_step
        with get_progress_bar(progress, total) as pbar:
            i = 0
            for _ in range(iterations):
                for _ in range(yield_step):
                    # Choose a random move
                    move = self._random.choice(self._moves, p=self._weights)

                    # Propose
                    state, accepted = move.propose(model, state)
                    state.random_state = self.random_state

                    if tune:
                        move.tune(state, accepted)

                    # Save the new step
                    if store and (i + 1) % checkpoint_step == 0:
                        self.backend.save_step(state, accepted)

                    pbar.update(1)
                    i += 1

                # Yield the result as an iterator so that the user can do all
                # sorts of fun stuff with the results so far.
                yield state

    def run_mcmc(self, initial_state, nsteps, **kwargs):
        """
        Iterate :func:`sample` for ``nsteps`` iterations and return the result

        Args:
            initial_state: The initial state or position vector. Can also be
                ``None`` to resume from where :func:``run_mcmc`` left off the
                last time it executed.
            nsteps: The number of steps to run.

        Other parameters are directly passed to :func:`sample`.

        This method returns the most recent result from :func:`sample`.

        """
        if initial_state is None:
            if self._previous_state is None:
                raise ValueError(
                    "Cannot have `initial_state=None` if run_mcmc has never "
                    "been called."
                )
            initial_state = self._previous_state

        results = None
        for results in self.sample(initial_state, iterations=nsteps, **kwargs):
            pass

        # Store so that the ``initial_state=None`` case will work
        self._previous_state = results

        return results

    def compute_log_prob(self, coords):
        """Calculate the vector of log-probability for the walkers

        Args:
            coords: (ndarray[..., ndim]) The position vector in parameter
                space where the probability should be calculated.

        This method returns:

        * log_prob: A vector of log-probabilities with one entry for each
          walker in this sub-ensemble.
        * blob: The list of meta data returned by the ``log_post_fn`` at
          this position or ``None`` if nothing was returned.

        """
        p = coords

        # Check that the parameters are in physical ranges.
        if np.any(np.isinf(p)):
            raise ValueError("At least one parameter value was infinite")
        if np.any(np.isnan(p)):
            raise ValueError("At least one parameter value was NaN")

        # Run the log-probability calculations (optionally in parallel).
        if self.vectorize:
            results = self.log_prob_fn(p)
        else:
            # If the `pool` property of the sampler has been set (i.e. we want
            # to use `multiprocessing`), use the `pool`'s map method.
            # Otherwise, just use the built-in `map` function.
            if self.pool is not None:
                map_func = self.pool.map
            else:
                map_func = map
            results = list(
                map_func(self.log_prob_fn, (p[i] for i in range(len(p))))
            )

        try:
            log_prob = np.array([float(l[0]) for l in results])
            blob = [l[1:] for l in results]
        except (IndexError, TypeError):
            log_prob = np.array([float(l) for l in results])
            blob = None
        else:
            # Get the blobs dtype
            if self.blobs_dtype is not None:
                dt = self.blobs_dtype
            else:
                try:
                    dt = np.atleast_1d(blob[0]).dtype
                except ValueError:
                    dt = np.dtype("object")
            blob = np.array(blob, dtype=dt)

            # Deal with single blobs properly
            shape = blob.shape[1:]
            if len(shape):
                axes = np.arange(len(shape))[np.array(shape) == 1] + 1
                if len(axes):
                    blob = np.squeeze(blob, tuple(axes))

        # Check for log_prob returning NaN.
        if np.any(np.isnan(log_prob)):
            raise ValueError("Probability function returned NaN")

        return log_prob, blob

    @property
    def acceptance_fraction(self):
        """The fraction of proposed steps that were accepted"""
        return self.backend.accepted / float(self.backend.iteration)

    @property
    @deprecated("get_chain()")
    def chain(self):  # pragma: no cover
        chain = self.get_chain()
        return np.swapaxes(chain, 0, 1)

    @property
    @deprecated("get_chain(flat=True)")
    def flatchain(self):  # pragma: no cover
        return self.get_chain(flat=True)

    @property
    @deprecated("get_log_prob()")
    def lnprobability(self):  # pragma: no cover
        log_prob = self.get_log_prob()
        return np.swapaxes(log_prob, 0, 1)

    @property
    @deprecated("get_log_prob(flat=True)")
    def flatlnprobability(self):  # pragma: no cover
        return self.get_log_prob(flat=True)

    @property
    @deprecated("get_blobs()")
    def blobs(self):  # pragma: no cover
        return self.get_blobs()

    @property
    @deprecated("get_blobs(flat=True)")
    def flatblobs(self):  # pragma: no cover
        return self.get_blobs(flat=True)

    @property
    @deprecated("get_autocorr_time")
    def acor(self):  # pragma: no cover
        return self.get_autocorr_time()

    def get_chain(self, **kwargs):
        return self.get_value("chain", **kwargs)

    get_chain.__doc__ = Backend.get_chain.__doc__

    def get_blobs(self, **kwargs):
        return self.get_value("blobs", **kwargs)

    get_blobs.__doc__ = Backend.get_blobs.__doc__

    def get_log_prob(self, **kwargs):
        return self.get_value("log_prob", **kwargs)

    get_log_prob.__doc__ = Backend.get_log_prob.__doc__

    def get_last_sample(self, **kwargs):
        return self.backend.get_last_sample()

    get_last_sample.__doc__ = Backend.get_last_sample.__doc__

    def get_value(self, name, **kwargs):
        return self.backend.get_value(name, **kwargs)

    def get_autocorr_time(self, **kwargs):
        return self.backend.get_autocorr_time(**kwargs)

    get_autocorr_time.__doc__ = Backend.get_autocorr_time.__doc__


class _FunctionWrapper(object):
    """
    This is a hack to make the likelihood function pickleable when ``args``
    or ``kwargs`` are also included.

    """

    def __init__(self, f, args, kwargs):
        self.f = f
        self.args = [] if args is None else args
        self.kwargs = {} if kwargs is None else kwargs

    def __call__(self, x):
        try:
            return self.f(x, *self.args, **self.kwargs)
        except:  # pragma: no cover
            import traceback

            print("emcee: Exception while calling your likelihood function:")
            print("  params:", x)
            print("  args:", self.args)
            print("  kwargs:", self.kwargs)
            print("  exception:")
            traceback.print_exc()
            raise
