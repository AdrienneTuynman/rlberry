import logging
import time

import numpy as np
from numba import jit

import rlberry.spaces as spaces
from rlberry.agents import Agent
from rlberry.agents.dynprog.utils import backward_induction, backward_induction_in_place
from rlberry.agents.utils.metrics import metric_lp
from rlberry.agents.kernel_based.kernels import kernel_func
from rlberry.envs import OnlineModel


@jit(nopython=True)
def map_to_representative(state,
                          lp_metric,
                          representative_states,
                          n_representatives,
                          min_dist,
                          scaling,
                          accept_new_repr):
    """
    Map state to representative state.
    """
    dist_to_closest = np.inf
    argmin = -1
    for ii in range(n_representatives):
        dist = metric_lp(state, representative_states[ii, :], lp_metric, scaling)
        if dist < dist_to_closest:
            dist_to_closest = dist
            argmin = ii

    max_representatives = representative_states.shape[0]
    if dist_to_closest > min_dist and n_representatives < max_representatives and accept_new_repr:
        new_index = n_representatives
        representative_states[new_index, :] = state
        return new_index
    return argmin


@jit(nopython=True)
def update_model(repr_state, action, repr_next_state, reward,
                 n_representatives, repr_states,
                 lp_metric, scaling, bandwidth, 
                 bonus_scale_factor, beta, v_max, bonus_type,
                 kernel_type, N_sa, B_sa, P_hat, R_hat):
    """
    Model update function, lots of arguments so we can use JIT :)
    """
     # aux var for transition update
    dirac_next_s = np.zeros(n_representatives)
    dirac_next_s[repr_next_state] = 1.0 

    for u_repr_state in range(n_representatives):
            # compute weight
            dist = metric_lp(repr_states[repr_state,   :],
                             repr_states[u_repr_state, :], 
                             lp_metric, 
                             scaling)
            weight  = kernel_func(dist/bandwidth, kernel_type=kernel_type)

            # aux variables
            prev_N_sa    = beta + N_sa[u_repr_state, action]  # regularization added here
            current_N_sa = prev_N_sa + weight

            # update weights
            N_sa[u_repr_state, action] += weight

            # update transitions
            P_hat[u_repr_state, action, :n_representatives] = dirac_next_s*weight / current_N_sa + \
                                                              (prev_N_sa/current_N_sa)*P_hat[u_repr_state, action, :n_representatives]            

            # update rewards
            R_hat[u_repr_state, action] = weight*reward/current_N_sa + (prev_N_sa/current_N_sa)*R_hat[u_repr_state, action]

            # update bonus
            B_sa[u_repr_state, action]    =  compute_bonus(N_sa[u_repr_state, action],
                                                           beta, bonus_scale_factor,
                                                           v_max, bonus_type)


@jit(nopython=True)
def compute_bonus(sum_weights, beta, bonus_scale_factor, v_max, bonus_type):
    n = beta + sum_weights
    if bonus_type == "simplified_bernstein":
        return bonus_scale_factor * np.sqrt(1.0/n) + (1+beta)*(v_max)/n
    else:
        raise NotImplementedError("Error: unknown bonus type.")


class RSKernelUCBVIAgent(Agent):
    """
    Implements KernelUCBVI [1] with representative states [2, 3].

    Value iteration with exploration bonuses for continuous-state environments,
    using a online discretization strategy + kernel smoothing:
    - Build (online) a set of representative states
    - Using smoothing kernels, estimate transtions an rewards on the finite set of representative states 
    and actions.

    Criterion: finite-horizon with discount factor gamma. 
    If the discount is not 1, only the Q function at h=0 is used.

    The recommended policy after all the episodes is computed without exploration bonuses.
        
    References
    ----------
    [1] Domingues et al., 2020
        Regret Bounds for Kernel-Based Reinforcement Learning
        https://arxiv.org/abs/2004.05599
    [2] Domingues et al., 2020
        A Kernel-Based Approach to Non-Stationary Reinforcement Learning in Metric Spaces 
        https://arxiv.org/abs/2007.05078
    [3] Kveton & Theocharous, 2012
        Kernel-Based Reinforcement Learning on Representative States
        https://www.aaai.org/ocs/index.php/AAAI/AAAI12/paper/viewFile/4967/5509
    """

    def __init__(self, env,
                 n_episodes=1000,
                 gamma=0.95,
                 horizon=None,
                 lp_metric=2,
                 kernel_type="epanechnikov",
                 scaling=None,
                 bandwidth= 0.05,
                 min_dist = 0.1,
                 max_repr=1000,
                 bonus_scale_factor=1.0,
                 beta = 0.01,
                 bonus_type="simplified_bernstein",
                 verbose=1,
                 **kwargs):
        """
        env : OnlineModel    
            Online model with continuous (Box) state space and discrete actions
        n_episodes : int
            number of episodes
        gamma : double 
            Discount factor in [0, 1]. If gamma is 1.0, the problem is set to be finite-horizon.
        horizon : int
            Horizon of the objective function. If None and gamma<1, set to 1/(1-gamma). 
        lp_metric: int
            The metric on the state space is the one induced by the p-norm,
            where p = lp_metric. Default = 2, for the Euclidean metric. 
        kernel_type : string
            See rlberry.agents.kernel_based.kernels.kernel_func for possible kernel types.
        scaling: numpy.ndarray
            Must have the same size as state array, used to scale the states before computing the metric.
            If None, set to:
            - (env.observation_space.high - env.observation_space.low) if high and low are bounded
            - np.ones(env.observation_space.dim) if high or low are unbounded
        bandwidth : double
            Kernel bandwidth.
        min_dist : double
            Minimum distance between two representative states
        max_repr : int
            Maximum number of representative states.
            If None, it is set to  (sqrt(d)/min_dist)**d, where d
            is the dimension of the state space
        bonus_scale_factor : double
            Constant by which to multiply the exploration bonus, controls 
            the level of exploration.
        beta : double
            Regularization constant.
        verbose : int
            Controls the verbosity, if non zero, progress messages are printed.   
        bonus_type : string
             Type of exploration bonus. Currently, only "simplified_bernstein"
             is implemented.
        """
        # init base class
        Agent.__init__(self, env, **kwargs)
        self.id = "RSKernelUCBVI"
        self.fit_info = ("n_episodes", "episode_rewards")

        self.n_episodes = n_episodes
        self.gamma = gamma
        self.horizon = horizon
        self.lp_metric = lp_metric
        self.kernel_type = kernel_type
        self.bandwidth = bandwidth
        self.min_dist = min_dist
        self.bonus_scale_factor = bonus_scale_factor
        self.beta = beta
        self.verbose = verbose
        self.bonus_type = bonus_type

        # check environment 
        assert isinstance(self.env, OnlineModel)
        assert isinstance(self.env.observation_space, spaces.Box)
        assert isinstance(self.env.action_space, spaces.Discrete)

        # other checks
        assert gamma >= 0 and gamma <= 1.0
        if self.horizon is None:
            assert gamma < 1.0, "If no horizon is given, gamma must be smaller than 1."
            self.horizon = int(np.ceil(1.0 / (1.0 - gamma)))

        # state dimension
        self.state_dim = self.env.observation_space.dim

        # compute scaling, if it is None
        if scaling is None:
            # if high and low are bounded
            if (self.env.observation_space.high == np.inf).sum() == 0 \
                    and (self.env.observation_space.low == -np.inf).sum() == 0:
                scaling = self.env.observation_space.high - self.env.observation_space.low
                # if high or low are unbounded
            else:
                scaling = np.ones(self.state_dim)
        else:
            assert scaling.ndim == 1
            assert scaling.shape[0] == self.state_dim
        self.scaling = scaling

        # maximum value 
        r_range = self.env.reward_range[1] - self.env.reward_range[0]
        if r_range == np.inf:
            logging.warning("%s: Reward range is infinity. Clipping it to 1." % self.id)
            r_range = 1.0

        if self.gamma == 1.0:
            self.v_max = r_range * horizon
        else:
            self.v_max = r_range * (1.0 - np.power(self.gamma, self.horizon)) / (1.0 - self.gamma)

        # number of representative states and number of actions
        if max_repr is None:
            max_repr = int(np.ceil((1.0 * np.sqrt(self.state_dim) / self.min_dist) ** self.state_dim))
        self.max_repr = max_repr

        # current number of representative states
        self.M = None
        self.A = self.env.action_space.n

        # declaring variables
        self.episode = None  # current episode
        self.representative_states = None  # array containing the coordinates of all representative statates
        self.N_sa = None   # sum of weights at (s, a)
        self.B_sa = None   # bonus at (s, a)
        self.R_hat = None  # reward  estimate
        self.P_hat = None  # transitions estimate
        self.Q = None  # Q function
        self.V = None  # V function

        self.Q_policy = None  # Q function for recommended policy

        # initialize
        self.reset()

    def reset(self, **kwargs):
        self.M = 0
        self.representative_states = np.zeros((self.max_repr, self.state_dim))
        self.N_sa = np.zeros((self.max_repr, self.A))
        self.B_sa = self.v_max * np.ones((self.max_repr, self.A))

        self.R_hat = np.zeros((self.max_repr, self.A))
        self.P_hat = np.zeros((self.max_repr, self.A, self.max_repr))

        self.V = np.zeros((self.horizon, self.max_repr))
        self.Q = np.zeros((self.horizon, self.max_repr, self.A))
        self.Q_policy = None

        self.episode = 0

        # logging config
        self._last_printed_ep = 0
        self._time_last_log = time.clock()
        if self.verbose == 1:
            self._log_interval = 60  # in seconds
        elif self.verbose == 2:
            self._log_interval = 30
        elif self.verbose == 3:
            self._log_interval = 15
        elif self.verbose > 3:
            self._log_interval = 5

    def policy(self, state, hh=0, **kwargs):
        assert self.Q_policy is not None
        repr_state = self._map_to_repr(state, False)

        # no discount
        if self.gamma == 1.0:
            return self.Q_policy[hh, repr_state, :].argmax()
        # discounted
        return self.Q_policy[0, repr_state, :].argmax()

    def fit(self, **kwargs):
        info = {}
        self._rewards = np.zeros(self.n_episodes)
        self._cumul_rewards = np.zeros(self.n_episodes)
        for kk in range(self.n_episodes):
            episode_rewards = self._run_episode()
            self._rewards[kk] = episode_rewards
            if kk > 0:
                self._cumul_rewards[kk] = episode_rewards + self._cumul_rewards[kk - 1]
            self.episode += 1
            # 
            self._logging()

        # compute Q function for the recommended policy
        self.Q_policy, _ = backward_induction(self.R_hat[:self.M, :],
                                              self.P_hat[:self.M, :, :self.M],
                                              self.horizon, self.gamma)

        info["n_episodes"] = self.n_episodes
        info["episode_rewards"] = self._rewards
        return info

    def _logging(self):
        if self.verbose > 0:
            t_now = time.clock()
            time_elapsed = t_now - self._time_last_log
            if time_elapsed >= self._log_interval:
                self._time_last_log = t_now
                print(self._info_to_print())
                self._last_printed_ep = self.episode - 1

    def _info_to_print(self):
        prev_episode = self._last_printed_ep
        episode = self.episode - 1
        reward_per_ep = self._rewards[prev_episode:episode + 1].sum() / max(1, episode - prev_episode)
        time_per_ep = self._log_interval * 1000.0 / max(1, episode - prev_episode)

        to_print = "[%s] episode = %d/%d | representative states = %d |reward/ep = %0.2f | time/ep = %0.2f ms" % (
            self.id, episode, self.n_episodes, self.M, reward_per_ep, time_per_ep)
        return to_print

    def _map_to_repr(self, state, accept_new_repr=True):
        repr_state = map_to_representative(state,
                                           self.lp_metric,
                                           self.representative_states,
                                           self.M,
                                           self.min_dist,
                                           self.scaling,
                                           accept_new_repr)
        # check if new representative state
        if repr_state == self.M:
            self.M += 1
        return repr_state

    def _update(self, state, action, next_state, reward):
        repr_state = self._map_to_repr(state)
        repr_next_state = self._map_to_repr(next_state)

        update_model(repr_state, action, repr_next_state, reward,
                     self.M, 
                     self.representative_states,
                     self.lp_metric,
                     self.scaling, 
                     self.bandwidth, 
                     self.bonus_scale_factor, 
                     self.beta, 
                     self.v_max, 
                     self.bonus_type,
                     self.kernel_type, 
                     self.N_sa, 
                     self.B_sa, 
                     self.P_hat, 
                     self.R_hat)

    def _get_action(self, state, hh=0):
        assert self.Q is not None
        repr_state = self._map_to_repr(state, False)

        # no discount
        if self.gamma == 1.0:
            return self.Q[hh, repr_state, :].argmax()
        # discounted
        return self.Q[0, repr_state, :].argmax()

    def _run_episode(self):
        # interact for H steps
        episode_rewards = 0
        state = self.env.reset()
        for hh in range(self.horizon):
            action = self._get_action(state, hh)
            next_state, reward, done, _ = self.env.step(action)
            self._update(state, action, next_state, reward)
            state = next_state
            episode_rewards += reward

        # run backward induction
        backward_induction_in_place(self.Q[:, :self.M, :], self.V[:, :self.M],
                                    self.R_hat[:self.M, :] + self.B_sa[:self.M, :],
                                    self.P_hat[:self.M, :, :self.M],
                                    self.horizon, self.gamma, self.v_max)

        # return sum of rewards collected in the episode
        return episode_rewards