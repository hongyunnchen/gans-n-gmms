import numpy as np
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
import pickle
import math
import os
import time
import multiprocessing

class Timer(object):
    def __init__(self, name='Operation'):
        self.name = name

    def __enter__(self):
        self.tstart = time.time()

    def __exit__(self, type, value, traceback):
        print('%s took: %s sec' % (self.name, time.time() - self.tstart))


class MFA:
    """
    Gaussian Mixture Model with optimization for High-dimensional Data
    In each component, the Covariance Matrix is approximated using:
    - A: a rectangular d x l matrix, where l <= d (typically l << d)
    - s: variance (sigma^2) of the added isotropic noise
    - Data is generated by transforming samples drawn from a standard normal random variable:
      - x = z_l @ A.T + z_d * s*np.eye() + mu
      - Two random sources are used: z_l (in the lower dimension l) and z_d in the full dimension
    - The covariance matrix Sigma = A @ A.T + s*np.eye()
    Note: The main idea is similar to tensorflow MultivariateNormalDiagPlusLowRank, except that TF
        decomposes the scale matrix A itself as low-rank plus diagonal.
    """
    def __init__(self, components=None):
        self.components = components
        self.eps = 1e-16
        self.max_l = 8

    def randomize_params(self, num_components, dim=2, low_rank_scale=0.1, noise_variance=0.01, mu_range=0.8,
                         isotropic_noise=False):
        # l is the smaller dimension of the non-square matrices. Typically l << dim
        l = min(self.max_l, dim)
        d = dim

        # Create the component selection probability vector
        pi = np.random.uniform(0.2, 1.0, num_components)
        pi = np.power(pi, 2)
        pi /= np.sum(pi)

        # Create the component parameters
        self.components = {}
        for i in range(num_components):
            # The diagonal component
            if isotropic_noise:
                D = np.random.uniform(low=noise_variance / 10.0, high=noise_variance) * np.ones(size=[d])
            else:
                D = np.random.uniform(low=noise_variance / 10.0, high=noise_variance, size=[d])
            # The rectangular scale matrix
            A = np.random.normal(scale=low_rank_scale, size=[d, l])
            mu = np.random.uniform(-mu_range, mu_range, dim)
            self.components[i] = {'D': D, 'A': A, 'mu': mu, 'pi': pi[i]}

    @staticmethod
    def _draw_from_component(num_samples, c, add_noise=True):
        d, l= c['A'].shape
        z_l = np.random.normal(size=[num_samples, l])
        # z_d @ np.diag(D) = z_d * D (element-wise multiply with broadcast) - column j of z_d is multiplied by Dj
        X = z_l @ c['A'].T + c['mu'].T
        if add_noise:
            z_d = np.random.normal(size=[num_samples, d])
            X += z_d * np.sqrt(c['D'])
        return X

    def draw_samples(self, num_samples, add_noise=True):
        pi = [c['pi'] for c in self.components.values()]
        # Fix numeric issue of pi not summing exactly to 1 (tensorflow softmax implementation?)
        sp = sum(pi)
        if not sp == 1.0:
            assert abs(sp-1.0) < 1e-5
            max_comp = pi.index(max(pi))
            pi[max_comp] -= (sp-1.0)

        # Choose components and then sample relevant points from each components
        samples = np.zeros((num_samples, self.components[0]['mu'].size), dtype=float)
        s_k = np.random.choice(len(pi), p=pi, size=num_samples)
        for k, c in self.components.items():
            s_k_i = (s_k == k)
            samples[s_k_i, :] = MFA._draw_from_component(sum(s_k_i), c, add_noise=add_noise)
        return samples

    @staticmethod
    def _get_component_log_probs_task(task):
        c = task['c']
        X = task['X']
        assert len(X.shape) == 2 and X.shape[1] == c['A'].shape[0]

        d, l = c['A'].shape
        invD = np.power(c['D'], -1.0).reshape([d, 1])

        # Calculate the inverse and determinant of sigma from the raw components using Woodbury's method
        IPiDP = np.eye(l) + c['A'].T @ (c['A'] * invD)

        # Note: inverse sigma calculation is fast, but takes O(d^2) memory
        # It should be possible to optimize (x-mu) @ iSigma_fast @ (x-mu)' directly
        inv_sigma1 = c['A'].T * invD.T
        inv_sigma2 = (np.linalg.inv(IPiDP) @ c['A'].T * invD.T).T

        # Calculate the determinant using the Matrix Determinant Lemma
        # See https://en.wikipedia.org/wiki/Matrix_determinant_lemma#Generalization
        log_dSigma = np.log(np.linalg.det(IPiDP)) + np.sum(np.log(c['D']))
        c_factor = d*np.log(2*np.pi) + log_dSigma

        # Calculate the log likelihood
        # The below formula was devised (by rearranging the products) to avoid multiplication by a (d x d) matrix
        X_c = X - c['mu']
        m_d = np.sum(X_c * (X_c * invD.T - (X_c @ inv_sigma2) @ inv_sigma1), axis=1)
        return task['comp_num'], np.log(c['pi']) - 0.5*(m_d + c_factor)


    def _get_component_log_probs(self, X, k):
        # TODO: Should add c['pi']
        c = self.components[k]
        assert len(X.shape) == 2 and X.shape[1] == c['A'].shape[0]

        d, l = c['A'].shape
        invD = np.power(c['D'], -1.0).reshape([d, 1])

        # Cache some calculations (that do not depend on x) for later re-use
        if 'c_factor' not in c.keys():

            # Calculate the inverse and determinant of sigma from the raw components using Woodbury's method
            IPiDP = np.eye(l) + c['A'].T @ (c['A'] * invD)

            # Note: inverse sigma calculation is fast, but takes O(d^2) memory
            # It should be possible to optimize (x-mu) @ iSigma_fast @ (x-mu)' directly
            c['inv_sigma1'] = c['A'].T * invD.T
            c['inv_sigma2'] = (np.linalg.inv(IPiDP) @ c['A'].T * invD.T).T

            # Calculate the determinant using the Matrix Determinant Lemma
            # See https://en.wikipedia.org/wiki/Matrix_determinant_lemma#Generalization
            log_dSigma = np.log(np.linalg.det(IPiDP)) + np.sum(np.log(c['D']))

            c['c_factor'] = d*np.log(2*np.pi) + log_dSigma
            self.components[k] = c

        # Calculate the log likelihood
        # The below formula was devised (by rearranging the products) to avoid multiplication by a (d x d) matrix
        X_c = X - c['mu']
        m_d = np.sum(X_c * (X_c * invD.T - (X_c @ c['inv_sigma2']) @ c['inv_sigma1']), axis=1)
        return -0.5 * (m_d + c['c_factor'])

    # Based on http://bayesjumping.net/log-sum-exp-trick/
    @staticmethod
    def _log_sum_exp(ns):
        max_vals = np.max(ns, axis=1, keepdims=True)
        sum_of_exp = np.sum(np.exp(ns - max_vals), axis=1, keepdims=True)
        return max_vals + np.log(sum_of_exp)

    def _rearrange_input(self, samples):
        """
        Input should be either a single data point of size d or a batch of m vectors i.e. size [m, d]
        """
        d = next(iter(self.components.values()))['A'].shape[0]
        if len(samples.shape) == 2:
            assert samples.shape[1] == d
            return samples
        assert samples.size == d
        return np.reshape(samples, [1, d])

    def _get_components_log_probabilities_multithreaded(self, samples):
        print('_get_components_log_probabilities - multhreaded start')
        X = self._rearrange_input(samples)
        components_log_probs = np.zeros([X.shape[0], len(self.components)], dtype=float)
        pool = multiprocessing.Pool(8)
        comonent_tasks = []
        for i in range(len(self.components)):
            comonent_tasks.append({'comp_num': i,
                                   'c': self.components[i],
                                   'X': X})
        comp_results = pool.map(MFA._get_component_log_probs_task, comonent_tasks)
        for comp_num, comp_ll in comp_results:
            components_log_probs[:, comp_num] = comp_ll
        print('_get_components_log_probabilities - multhreaded end')
        return components_log_probs


    def _get_components_log_probabilities(self, samples):
        X = self._rearrange_input(samples)
        components_log_probs = np.zeros([X.shape[0], len(self.components)], dtype=float)
        for k, c in self.components.items():
            components_log_probs[:, k] = np.log(c['pi']) + self._get_component_log_probs(X, k)
        return components_log_probs

    def _get_components_log_probabilities_debug(self, samples):
        X = self._rearrange_input(samples)
        components_log_probs = np.zeros([X.shape[0], len(self.components)], dtype=float)
        for k, c in self.components.items():
            components_log_probs[:, k] = self._get_component_log_probs(X, k)
        return components_log_probs

    def get_log_probabilities(self, samples):
        return MFA._log_sum_exp(self._get_components_log_probabilities(samples))

    def get_log_likelihood(self, samples):
        return np.sum(self.get_log_probabilities(samples))

    def get_probabilities(self, samples):
        return np.exp(self.get_log_probabilities(samples))

    def get_log_responsibilities(self, samples):
        per_comp_log_probs = self._get_components_log_probabilities(samples)
        return per_comp_log_probs - MFA._log_sum_exp(per_comp_log_probs)

    def get_responsibilities(self, samples):
        return np.exp(self.get_log_responsibilities(samples))

    # @staticmethod
    # def calculate_posterior(c, X):
    #     """
    #     Calculate the Probabilistic PCA posterior (p(x|t) in the paper terms)
    #     """
    #     d, l = c['A'].shape
    #     invD = np.power(c['D'], -1.0).reshape([d, 1])
    #     invL = np.linalg.inv(np.eye(l) + c['A'].T @ (c['A'] * invD))
    #     X_c = X - c['mu']
    #
    #     invS1 = c['A'].T  * invD.T
    #     invS2 = (invL @ c['A'].T * invD.T).T
    #
    #     X_c * invD.T - (X_c @ invS2) @ invS1
    #
    #
    #     # inv_D = np.power(c['D'], -1)
    #     # inv_L = np.linalg.inv(np.eye(l) + c['A'].T @ inv_D @ c['A'])
    #     # inv_M = np.linalg.inv(c['D'] + c['A'].T @ c['A'])
    #     # p_mu = inv_M @ c['A'].T @ (x - c['mu'])
    #     # p_sigma = ()
    #     # return p_mu, p_sigma
    #
    # @staticmethod
    # def posterior_mode_to_sample(c, z):
    #     return c['A'] @ z + c['mu']

    def plot_components(self, num_samples=None, figure_num=1, subplot=111, title=None):
        fig = plt.figure(figure_num)
        if self.components[0]['mu'].size < 3:
            ax = fig.add_subplot(subplot)
        else:
            ax = fig.add_subplot(subplot, projection='3d')
        plt.cla()
        if not num_samples:
            num_samples = 1000 * len(self.components)
        for c in self.components.values():
            P = MFA._draw_from_component(int(c['pi'] * num_samples), c)
            if P.shape[1] == 2:
                ax.plot(P[:,0], P[:,1], '.', alpha=0.3)
                plt.axis([-1.2, 1.2, -1.2, 1.2])
            else:
                ax.plot(P[:,0], P[:,1], P[:,2], '.', alpha=0.3)
                ax.set_xlim3d(-1.2, 1.2)
                ax.set_ylim3d(-1.2, 1.2)
                ax.set_zlim3d(-1.2, 1.2)
        plt.grid(True)
        if title:
            plt.title(title)

    def plot_samples(self, samples, component_nums=None, figure_num=1, subplot=111, title=None):
        if not component_nums:
            # Find most probable component for each sample
            component_nums = np.argmax(self.get_responsibilities(samples), axis=1)
        fig = plt.figure(figure_num)
        if self.components[0]['mu'].size < 3:
            ax = fig.add_subplot(subplot)
        else:
            ax = fig.add_subplot(subplot, projection='3d')
        plt.cla()
        component_nums = np.array(component_nums)
        for c in self.components.keys():
            if samples.shape[1] == 2:
                ax.plot(samples[component_nums == c, 0], samples[component_nums == c, 1], '.', alpha=0.3)
                plt.axis([-1.2, 1.2, -1.2, 1.2])
            else:
                ax.plot(samples[component_nums == c, 0], samples[component_nums == c, 1],
                        samples[component_nums == c, 2], '.', alpha=0.3)
                ax.set_xlim3d(-1.2, 1.2)
                ax.set_ylim3d(-1.2, 1.2)
                ax.set_zlim3d(-1.2, 1.2)
        if title:
            plt.title(title)
        plt.grid(True)

    def save(self, file_name):
        with open(file_name+'.pkl', 'wb') as f:
            pickle.dump(self.components, f, pickle.HIGHEST_PROTOCOL)

    def load(self, file_name):
        with open(file_name+'.pkl', 'rb') as f:
            self.components = pickle.load(f)
        # Backwards compatibility...
        for k in self.components.keys():
            self.components[k]['mu'] = np.array(self.components[k]['mu'])


if __name__ == "__main__":
    try_gmm = MFA()
    print('randomizing...')
    try_gmm.randomize_params(3, 500, low_rank_scale=0.2, noise_variance=0.01, mu_range=0.2)
    #
    # print('drawing samples...')
    # X = try_gmm.draw_samples(5000)
    #
    # print('Calculating samples log likelihood - method 2...')
    # ll2 = try_gmm.get_log_likelihood(X)
    # print('Log likelihood =', ll2)
    #
    print('plotting components...')
    try_gmm.plot_components()
    #
    # print('plotting samples...')
    # try_gmm.plot_samples(X, figure_num=2)
    plt.show()
    #
    output_folder = '../../../Data/very_high_dim_data'
    model_name = 'ref_gmm_3C_500D_t02'
    num_train_samples = 100000
    print('drawing training samples...')
    train_sample = try_gmm.draw_samples(num_train_samples)
    ll = try_gmm.get_log_likelihood(train_sample)
    print('Log likelihood =', ll)
    print('drawing test samples...')
    test_sample = try_gmm.draw_samples(int(num_train_samples/4))
    # print('saving...')
    # try_gmm.save(os.path.join(output_folder, model_name))
    # np.save(os.path.join(output_folder, model_name+'_train.npy'), train_sample)
    # np.save(os.path.join(output_folder, model_name+'_test.npy'), test_sample)
