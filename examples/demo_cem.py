import numpy as np
from rlberry.agents.cem import CEMAgent
from rlberry.envs.toy_exploration import PBall2D

env = PBall2D(p=np.inf, reward_smoothness=np.array([0.8]), reward_centers=[np.array([0.4, 0.4])])
n_batches  = 50
batch_size = 16
horizon = 25
gamma = 0.99

agent = CEMAgent(env, gamma, horizon, batch_size, n_batches, percentile=70, learning_rate=0.01)
agent.fit()

   
env.enable_rendering()
state = env.reset()
for tt in range(4*horizon):
    action = agent.policy(state)
    next_state, reward, done, _ = env.step(action)
    state = next_state

env.render()