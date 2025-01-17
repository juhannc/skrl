from typing import Union, Tuple, Dict, Any

import gym
import copy

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils.convert_parameters import parameters_to_vector
from torch.nn.utils.convert_parameters import vector_to_parameters

from ....memories.torch import Memory
from ....models.torch import Model

from .. import Agent


TRPO_DEFAULT_CONFIG = {
    "rollouts": 16,                 # number of rollouts before updating
    "learning_epochs": 8,           # number of learning epochs during each update
    "mini_batches": 2,              # number of mini batches during each learning epoch
    
    "discount_factor": 0.99,        # discount factor (gamma)
    "lambda": 0.99,                 # TD(lambda) coefficient (lam) for computing returns and advantages
    
    "value_learning_rate": 1e-3,    # value learning rate
    "learning_rate_scheduler": None,        # learning rate scheduler class (see torch.optim.lr_scheduler)
    "learning_rate_scheduler_kwargs": {},   # learning rate scheduler's kwargs (e.g. {"step_size": 1e-3})

    "state_preprocessor": None,             # state preprocessor class (see skrl.resources.preprocessors)
    "state_preprocessor_kwargs": {},        # state preprocessor's kwargs (e.g. {"size": env.observation_space})
    "value_preprocessor": None,             # value preprocessor class (see skrl.resources.preprocessors)
    "value_preprocessor_kwargs": {},        # value preprocessor's kwargs (e.g. {"size": 1})

    "random_timesteps": 0,          # random exploration steps
    "learning_starts": 0,           # learning starts after this many steps

    "grad_norm_clip": 0.5,          # clipping coefficient for the norm of the gradients
    "value_loss_scale": 1.0,        # value loss scaling factor

    "damping": 0.1,                     # damping coefficient for computing the Hessian-vector product
    "max_kl_divergence": 0.01,          # maximum KL divergence between old and new policy
    "conjugate_gradient_steps": 10,     # maximum number of iterations for the conjugate gradient algorithm
    "max_backtrack_steps": 10,          # maximum number of backtracking steps during line search
    "accept_ratio": 0.5,                # accept ratio for the line search loss improvement
    "step_fraction": 1.0,               # fraction of the step size for the line search

    "rewards_shaper": None,         # rewards shaping function: Callable(reward, timestep, timesteps) -> reward

    "experiment": {
        "directory": "",            # experiment's parent directory
        "experiment_name": "",      # experiment name
        "write_interval": 250,      # TensorBoard writing interval (timesteps)

        "checkpoint_interval": 1000,        # interval for checkpoints (timesteps)
        "store_separately": False,          # whether to store checkpoints separately
    }
}


class TRPO(Agent):
    def __init__(self, 
                 models: Dict[str, Model], 
                 memory: Union[Memory, Tuple[Memory], None] = None, 
                 observation_space: Union[int, Tuple[int], gym.Space, None] = None, 
                 action_space: Union[int, Tuple[int], gym.Space, None] = None, 
                 device: Union[str, torch.device] = "cuda:0", 
                 cfg: dict = {}) -> None:
        """Trust Region Policy Optimization (TRPO)

        https://arxiv.org/abs/1502.05477
        
        :param models: Models used by the agent
        :type models: dictionary of skrl.models.torch.Model
        :param memory: Memory to storage the transitions.
                       If it is a tuple, the first element will be used for training and 
                       for the rest only the environment transitions will be added
        :type memory: skrl.memory.torch.Memory, list of skrl.memory.torch.Memory or None
        :param observation_space: Observation/state space or shape (default: None)
        :type observation_space: int, tuple or list of integers, gym.Space or None, optional
        :param action_space: Action space or shape (default: None)
        :type action_space: int, tuple or list of integers, gym.Space or None, optional
        :param device: Computing device (default: "cuda:0")
        :type device: str or torch.device, optional
        :param cfg: Configuration dictionary
        :type cfg: dict

        :raises KeyError: If the models dictionary is missing a required key
        """
        _cfg = copy.deepcopy(TRPO_DEFAULT_CONFIG)
        _cfg.update(cfg)
        super().__init__(models=models, 
                         memory=memory, 
                         observation_space=observation_space, 
                         action_space=action_space, 
                         device=device, 
                         cfg=_cfg)

        # models
        self.policy = self.models.get("policy", None)
        self.value = self.models.get("value", None)

        self.backup_policy = copy.deepcopy(self.policy)

        # checkpoint models
        self.checkpoint_modules["policy"] = self.policy
        self.checkpoint_modules["value"] = self.value

        # configuration
        self._learning_epochs = self.cfg["learning_epochs"]
        self._mini_batches = self.cfg["mini_batches"]
        self._rollouts = self.cfg["rollouts"]
        self._rollout = 0

        self._grad_norm_clip = self.cfg["grad_norm_clip"]
        self._value_loss_scale = self.cfg["value_loss_scale"]

        self._max_kl_divergence = self.cfg["max_kl_divergence"]
        self._damping = self.cfg["damping"]
        self._conjugate_gradient_steps = self.cfg["conjugate_gradient_steps"]
        self._max_backtrack_steps = self.cfg["max_backtrack_steps"]
        self._accept_ratio = self.cfg["accept_ratio"]
        self._step_fraction = self.cfg["step_fraction"]

        self._value_learning_rate = self.cfg["value_learning_rate"]
        self._learning_rate_scheduler = self.cfg["learning_rate_scheduler"]

        self._state_preprocessor = self.cfg["state_preprocessor"]
        self._value_preprocessor = self.cfg["value_preprocessor"]

        self._discount_factor = self.cfg["discount_factor"]
        self._lambda = self.cfg["lambda"]

        self._random_timesteps = self.cfg["random_timesteps"]
        self._learning_starts = self.cfg["learning_starts"]

        self._rewards_shaper = self.cfg["rewards_shaper"]

        # set up optimizer and learning rate scheduler
        if self.policy is not None and self.value is not None:
            self.value_optimizer = torch.optim.Adam(self.value.parameters(), lr=self._value_learning_rate)
            if self._learning_rate_scheduler is not None:
                self.value_scheduler = self._learning_rate_scheduler(self.value_optimizer, **self.cfg["learning_rate_scheduler_kwargs"])

            self.checkpoint_modules["value_optimizer"] = self.value_optimizer

        # set up preprocessors
        if self._state_preprocessor:
            self._state_preprocessor = self._state_preprocessor(**self.cfg["state_preprocessor_kwargs"])
            self.checkpoint_modules["state_preprocessor"] = self._state_preprocessor
        else:
            self._state_preprocessor = self._empty_preprocessor

        if self._value_preprocessor:
            self._value_preprocessor = self._value_preprocessor(**self.cfg["value_preprocessor_kwargs"])
            self.checkpoint_modules["value_preprocessor"] = self._value_preprocessor
        else:
            self._value_preprocessor = self._empty_preprocessor

    def init(self) -> None:
        """Initialize the agent
        """
        super().init()
        
        # create tensors in memory
        if self.memory is not None:
            self.memory.create_tensor(name="states", size=self.observation_space, dtype=torch.float32)
            self.memory.create_tensor(name="actions", size=self.action_space, dtype=torch.float32)
            self.memory.create_tensor(name="rewards", size=1, dtype=torch.float32)
            self.memory.create_tensor(name="dones", size=1, dtype=torch.bool)
            self.memory.create_tensor(name="log_prob", size=1, dtype=torch.float32)
            self.memory.create_tensor(name="values", size=1, dtype=torch.float32)
            self.memory.create_tensor(name="returns", size=1, dtype=torch.float32)
            self.memory.create_tensor(name="advantages", size=1, dtype=torch.float32)

        self.tensors_names = ["states", "actions", "log_prob", "returns", "advantages"]

        # create temporary variables needed for storage and computation
        self._current_log_prob = None
        self._current_next_states = None

    def act(self, states: torch.Tensor, timestep: int, timesteps: int) -> torch.Tensor:
        """Process the environment's states to make a decision (actions) using the main policy

        :param states: Environment's states
        :type states: torch.Tensor
        :param timestep: Current timestep
        :type timestep: int
        :param timesteps: Number of timesteps
        :type timesteps: int

        :return: Actions
        :rtype: torch.Tensor
        """
        states = self._state_preprocessor(states)

        # sample random actions
        # TODO, check for stochasticity
        if timestep < self._random_timesteps:
            return self.policy.random_act(states, taken_actions=None, role="policy")

        # sample stochastic actions
        actions, log_prob, actions_mean = self.policy.act(states, taken_actions=None, role="policy")
        self._current_log_prob = log_prob

        return actions, log_prob, actions_mean

    def record_transition(self, 
                          states: torch.Tensor, 
                          actions: torch.Tensor, 
                          rewards: torch.Tensor, 
                          next_states: torch.Tensor, 
                          dones: torch.Tensor, 
                          infos: Any, 
                          timestep: int, 
                          timesteps: int) -> None:
        """Record an environment transition in memory
        
        :param states: Observations/states of the environment used to make the decision
        :type states: torch.Tensor
        :param actions: Actions taken by the agent
        :type actions: torch.Tensor
        :param rewards: Instant rewards achieved by the current actions
        :type rewards: torch.Tensor
        :param next_states: Next observations/states of the environment
        :type next_states: torch.Tensor
        :param dones: Signals to indicate that episodes have ended
        :type dones: torch.Tensor
        :param infos: Additional information about the environment
        :type infos: Any type supported by the environment
        :param timestep: Current timestep
        :type timestep: int
        :param timesteps: Number of timesteps
        :type timesteps: int
        """
        super().record_transition(states, actions, rewards, next_states, dones, infos, timestep, timesteps)

        # reward shaping
        if self._rewards_shaper is not None:
            rewards = self._rewards_shaper(rewards, timestep, timesteps)

        self._current_next_states = next_states

        if self.memory is not None:
            with torch.no_grad():
                values, _, _ = self.value.act(states=self._state_preprocessor(states), taken_actions=None, role="value")
            values = self._value_preprocessor(values, inverse=True)

            self.memory.add_samples(states=states, actions=actions, rewards=rewards, next_states=next_states, dones=dones, 
                                    log_prob=self._current_log_prob, values=values)
            for memory in self.secondary_memories:
                memory.add_samples(states=states, actions=actions, rewards=rewards, next_states=next_states, dones=dones, 
                                   log_prob=self._current_log_prob, values=values)

    def pre_interaction(self, timestep: int, timesteps: int) -> None:
        """Callback called before the interaction with the environment

        :param timestep: Current timestep
        :type timestep: int
        :param timesteps: Number of timesteps
        :type timesteps: int
        """
        pass

    def post_interaction(self, timestep: int, timesteps: int) -> None:
        """Callback called after the interaction with the environment

        :param timestep: Current timestep
        :type timestep: int
        :param timesteps: Number of timesteps
        :type timesteps: int
        """
        self._rollout += 1
        if not self._rollout % self._rollouts and timestep >= self._learning_starts:
            self._update(timestep, timesteps)

        # write tracking data and checkpoints
        super().post_interaction(timestep, timesteps)

    def _update(self, timestep: int, timesteps: int) -> None:
        """Algorithm's main update step

        :param timestep: Current timestep
        :type timestep: int
        :param timesteps: Number of timesteps
        :type timesteps: int
        """
        def compute_gae(rewards: torch.Tensor, 
                        dones: torch.Tensor, 
                        values: torch.Tensor, 
                        next_values: torch.Tensor, 
                        discount_factor: float = 0.99, 
                        lambda_coefficient: float = 0.95) -> torch.Tensor:
            """Compute the Generalized Advantage Estimator (GAE)

            :param rewards: Rewards obtained by the agent
            :type rewards: torch.Tensor
            :param dones: Signals to indicate that episodes have ended
            :type dones: torch.Tensor
            :param values: Values obtained by the agent
            :type values: torch.Tensor
            :param next_values: Next values obtained by the agent
            :type next_values: torch.Tensor
            :param discount_factor: Discount factor
            :type discount_factor: float
            :param lambda_coefficient: Lambda coefficient
            :type lambda_coefficient: float

            :return: Generalized Advantage Estimator
            :rtype: torch.Tensor
            """
            advantage = 0
            advantages = torch.zeros_like(rewards)
            not_dones = dones.logical_not()
            memory_size = rewards.shape[0]

            # advantages computation
            for i in reversed(range(memory_size)):
                next_values = values[i + 1] if i < memory_size - 1 else last_values
                advantage = rewards[i] - values[i] + discount_factor * not_dones[i] * (next_values + lambda_coefficient * advantage)
                advantages[i] = advantage
            # returns computation
            returns = advantages + values
            # normalize advantages
            advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)

            return returns, advantages

        def surrogate_loss(policy: Model, 
                           states: torch.Tensor, 
                           actions: torch.Tensor, 
                           log_prob: torch.Tensor, 
                           advantages: torch.Tensor) -> torch.Tensor:
            """Compute the surrogate objective (policy loss)

            :param policy: Policy
            :type policy: Model
            :param states: States
            :type states: torch.Tensor
            :param actions: Actions
            :type actions: torch.Tensor
            :param log_prob: Log probability
            :type log_prob: torch.Tensor
            :param advantages: Advantages
            :type advantages: torch.Tensor

            :return: Surrogate loss
            :rtype: torch.Tensor
            """
            _, new_log_prob, _ = policy.act(states, taken_actions=actions, role="policy")
            return (advantages * torch.exp(new_log_prob - log_prob.detach())).mean()

        def conjugate_gradient(policy: Model, 
                               states: torch.Tensor, 
                               b: torch.Tensor, 
                               num_iterations: float = 10, 
                               residual_tolerance: float = 1e-10) -> torch.Tensor:
            """Conjugate gradient algorithm to solve Ax = b using the iterative method

            https://en.wikipedia.org/wiki/Conjugate_gradient_method#As_an_iterative_method

            :param policy: Policy
            :type policy: Model
            :param states: States
            :type states: torch.Tensor
            :param b: Vector b 
            :type b: torch.Tensor
            :param num_iterations: Number of iterations (default: 10)
            :type num_iterations: float, optional
            :param residual_tolerance: Residual tolerance (default: 1e-10)
            :type residual_tolerance: float, optional

            :return: Conjugate vector
            :rtype: torch.Tensor
            """
            x = torch.zeros_like(b)
            r = b.clone()
            p = b.clone()
            rr_old = torch.dot(r, r)
            for _ in range(num_iterations):
                hv = fisher_vector_product(policy, states, p, damping=self._damping)
                alpha = rr_old / torch.dot(p, hv)
                x += alpha * p
                r -= alpha * hv
                rr_new = torch.dot(r, r)
                if rr_new < residual_tolerance:
                    break
                p = r + rr_new / rr_old * p
                rr_old = rr_new
            return x

        def fisher_vector_product(policy: Model, 
                                  states: torch.Tensor, 
                                  vector: torch.Tensor, 
                                  damping: float = 0.1) -> torch.Tensor:
            """Compute the Fisher vector product (direct method)
            
            https://www.telesens.co/2018/06/09/efficiently-computing-the-fisher-vector-product-in-trpo/

            :param policy: Policy
            :type policy: Model
            :param states: States
            :type states: torch.Tensor
            :param vector: Vector
            :type vector: torch.Tensor
            :param damping: Damping (default: 0.1)
            :type damping: float, optional

            :return: Hessian vector product
            :rtype: torch.Tensor
            """
            kl = kl_divergence(policy, policy, states)
            kl_gradient = torch.autograd.grad(kl, policy.parameters(), create_graph=True)
            flat_kl_gradient = torch.cat([gradient.view(-1) for gradient in kl_gradient])
            hessian_vector_gradient = torch.autograd.grad((flat_kl_gradient * vector).sum(), policy.parameters())
            flat_hessian_vector_gradient = torch.cat([gradient.contiguous().view(-1) for gradient in hessian_vector_gradient])
            return flat_hessian_vector_gradient + damping * vector

        def kl_divergence(policy_1: Model, policy_2: Model, states: torch.Tensor) -> torch.Tensor:
            """Compute the KL divergence between two distributions

            https://en.wikipedia.org/wiki/Normal_distribution#Other_properties

            :param policy_1: First policy
            :type policy_1: Model
            :param policy_2: Second policy
            :type policy_2: Model
            :param states: States
            :type states: torch.Tensor

            :return: KL divergence
            :rtype: torch.Tensor
            """
            _, _, mu_1 = policy_1.act(states, taken_actions=None, role="policy")
            logstd_1 = policy_1.get_log_std(role="policy")
            mu_1, logstd_1 = mu_1.detach(), logstd_1.detach()

            _, _, mu_2 = policy_2.act(states, taken_actions=None, role="policy")
            logstd_2 = policy_2.get_log_std(role="policy")
            
            kl = logstd_1 - logstd_2 + 0.5 * (torch.square(logstd_1.exp()) + torch.square(mu_1 - mu_2)) \
               / torch.square(logstd_2.exp()) - 0.5
            return torch.sum(kl, dim=-1).mean()

        # compute returns and advantages
        with torch.no_grad():
            last_values, _, _ = self.value.act(self._state_preprocessor(self._current_next_states.float()), taken_actions=None, role="value")
        last_values = self._value_preprocessor(last_values, inverse=True)
        
        values = self.memory.get_tensor_by_name("values")
        returns, advantages = compute_gae(rewards=self.memory.get_tensor_by_name("rewards"),
                                          dones=self.memory.get_tensor_by_name("dones"),
                                          values=values,
                                          next_values=last_values,
                                          discount_factor=self._discount_factor,
                                          lambda_coefficient=self._lambda)

        self.memory.set_tensor_by_name("values", self._value_preprocessor(values, train=True))
        self.memory.set_tensor_by_name("returns", self._value_preprocessor(returns, train=True))
        self.memory.set_tensor_by_name("advantages", advantages)

        # sample mini-batches from memory
        sampled_batches = self.memory.sample_all(names=self.tensors_names, mini_batches=self._mini_batches)

        cumulative_policy_loss = 0
        cumulative_value_loss = 0

        # learning epochs
        for epoch in range(self._learning_epochs):
            
            # mini-batches loop
            for sampled_states, sampled_actions, sampled_log_prob, sampled_returns, sampled_advantages in sampled_batches:

                sampled_states = self._state_preprocessor(sampled_states, train=not epoch)

                # compute policy loss gradient
                policy_loss = surrogate_loss(self.policy, sampled_states, sampled_actions, sampled_log_prob, sampled_advantages)
                policy_loss_gradient = torch.autograd.grad(policy_loss, self.policy.parameters())
                flat_policy_loss_gradient = torch.cat([gradient.view(-1) for gradient in policy_loss_gradient])

                # compute the search direction using the conjugate gradient algorithm
                search_direction = conjugate_gradient(self.policy, sampled_states, flat_policy_loss_gradient.data, 
                                                      num_iterations=self._conjugate_gradient_steps)

                # compute step size and full step
                xHx = (search_direction * fisher_vector_product(self.policy, sampled_states, search_direction, self._damping)) \
                    .sum(0, keepdim=True)
                step_size = torch.sqrt(2 * self._max_kl_divergence / xHx)[0]
                full_step = step_size * search_direction

                # backtracking line search
                restore_policy_flag = True
                self.backup_policy.update_parameters(self.policy)
                params = parameters_to_vector(self.policy.parameters())

                expected_improvement = (flat_policy_loss_gradient * full_step).sum(0, keepdim=True)

                for alpha in [self._step_fraction * 0.5 ** i for i in range(self._max_backtrack_steps)]:
                    new_params = params + alpha * full_step
                    vector_to_parameters(new_params, self.policy.parameters())

                    expected_improvement *= alpha
                    kl = kl_divergence(self.backup_policy, self.policy, sampled_states)
                    loss = surrogate_loss(self.policy, sampled_states, sampled_actions, sampled_log_prob, sampled_advantages)

                    if kl < self._max_kl_divergence and (loss - policy_loss) / expected_improvement > self._accept_ratio:
                        restore_policy_flag = False
                        break

                if restore_policy_flag:
                    self.policy.update_parameters(self.backup_policy)

                # compute value loss
                predicted_values, _, _ = self.value.act(sampled_states, taken_actions=None, role="value")

                value_loss = self._value_loss_scale * F.mse_loss(sampled_returns, predicted_values)

                # optimization step (value)
                self.value_optimizer.zero_grad()
                value_loss.backward()
                if self._grad_norm_clip > 0:
                    nn.utils.clip_grad_norm_(self.value.parameters(), self._grad_norm_clip)
                self.value_optimizer.step()

                # update cumulative losses
                cumulative_policy_loss += policy_loss.item()
                cumulative_value_loss += value_loss.item()

            # update learning rate
            if self._learning_rate_scheduler:
                self.value_scheduler.step()

        # record data
        self.track_data("Loss / Policy loss", cumulative_policy_loss / (self._learning_epochs * self._mini_batches))
        self.track_data("Loss / Value loss", cumulative_value_loss / (self._learning_epochs * self._mini_batches))
        
        self.track_data("Policy / Standard deviation", self.policy.distribution(role="policy").stddev.mean().item())

        if self._learning_rate_scheduler:
            self.track_data("Learning / Value learning rate", self.value_scheduler.get_last_lr()[0])
