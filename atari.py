import gym
import numpy as np
import imutil
import time
from concurrent import futures


def map_fn(fn, *iterables):
    with futures.ThreadPoolExecutor(max_workers=4) as executor:
        result_iterator = executor.map(fn, *iterables)
    return [i for i in result_iterator]


class MultiEnvironment():
    def __init__(self, name, batch_size):
        start_time = time.time()
        self.batch_size = batch_size
        #self.envs = map_fn(lambda idx: gym.make(name), range(batch_size))
        # ALE is non-threadsafe
        self.envs = [gym.make(name) for i in range(batch_size)]
        self.reset()
        print('Initialized {} environments in {:.03f}s'.format(self.batch_size, time.time() - start_time))

    def reset(self):
        for env in self.envs:
            reset_env(env)

    def step(self, actions):
        start_time = time.time()
        assert len(actions) == len(self.envs)

        def run_one_step(env, action):
            state, reward, done, info = env.step(action)
            if done:
                reset_env(env)
            return state, reward, done, info

        results = map_fn(run_one_step, self.envs, actions)
        #print('Ran {} environments one step in {:.03f}s'.format(self.batch_size, time.time() - start_time))
        states, rewards, dones, infos = zip(*results)
        return states, rewards, dones, infos

def reset_env(env):
    # Pong: wait until the enemy paddle appears
    env.reset()
    for _ in range(100):
        env.step(0)


if __name__ == '__main__':
    batch_size = 64
    env = MultiEnvironment('Pong-v0', batch_size)
    for i in range(10):
        actions = np.random.randint(0, 4, size=batch_size)
        states, rewards, dones, infos = env.step(actions)
        import imutil
        imutil.show(states[:4])


