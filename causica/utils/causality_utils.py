from typing import List, Optional, Tuple, Union

import numpy as np
import scipy
import torch

from ..datasets.intervention_data import InterventionData
from ..datasets.variables import Variables
from ..models.imodel import IModelForCausalInference, IModelForCounterfactuals, IModelForInterventions
from ..utils.helper_functions import to_tensors
from ..utils.torch_utils import LinearModel, MultiROFFeaturiser
from .evaluation_dataclasses import AteRMSEMetrics, IteRMSEMetrics, TreatmentDataLogProb


def intervene_graph(adj_matrix: torch.Tensor, intervention_idxs: torch.Tensor, copy_graph: bool = True) -> torch.Tensor:
    """
    Simulates an intervention by removing all incoming edges for nodes being intervened

    Args:
        adj_matrix: torch.Tensor of shape (input_dim, input_dim) containing  adjacency_matrix
        intervention_idxs: torch.Tensor containing which variables to intervene
        copy_graph: bool whether the operation should be performed in-place or a new matrix greated
    """
    if intervention_idxs is None or len(intervention_idxs) == 0:
        return adj_matrix

    if copy_graph:
        adj_matrix = adj_matrix.clone()

    adj_matrix[:, intervention_idxs] = 0
    return adj_matrix


def intervention_to_tensor(intervention_idxs, intervention_values, group_mask, device):
    """
    Maps empty interventions to nan and np.ndarray intervention data to torch tensors.
    Converts indices to a mask using the group_mask.
    """
    intervention_mask = None

    if intervention_idxs is not None and intervention_values is not None:
        (intervention_idxs,) = to_tensors(intervention_idxs, device=device, dtype=torch.long)
        (intervention_values,) = to_tensors(intervention_values, device=device, dtype=torch.float)

        if intervention_idxs.dim() == 0:
            intervention_idxs = None

        if intervention_values.dim() == 0:
            intervention_values = None

        intervention_mask = get_mask_from_idxs(intervention_idxs, group_mask, device)

    return intervention_idxs, intervention_mask, intervention_values


def get_mask_from_idxs(idxs, group_mask, device):
    """
    Generate mask for observations or samples from indices using group_mask
    """
    mask = torch.zeros(group_mask.shape[0], device=device, dtype=torch.bool)
    mask[idxs] = 1
    (group_mask,) = to_tensors(group_mask, device=device, dtype=torch.bool)
    mask = (mask.unsqueeze(1) * group_mask).sum(0).bool()
    return mask


def get_treatment_data_logprob(
    model: IModelForCausalInference,
    intervention_datasets: List[InterventionData],
    most_likely_graph: bool = False,
) -> TreatmentDataLogProb:
    """
    Computes the log-probability of test-points sampled from intervened distributions.
    Args:
        model: IModelForInterventions with which we can evaluate the log-probability of points while applying interventions to the generative model
        intervention_datasets: List[InterventionData] containing intervetions and samples from the ground truth data generating process when the intervention is applied
        most_likely_graph: whether to use the most likely causal graph (True) or to sample graphs (False)
    Returns:
        Summary statistics about the log probabilities from the intervened distributions
    """
    all_log_probs = []
    assert isinstance(model, IModelForInterventions)
    for intervention_data in intervention_datasets:

        assert intervention_data.intervention_values is not None
        intervention_log_probs = model.log_prob(
            X=intervention_data.test_data.astype(float),
            most_likely_graph=most_likely_graph,
            intervention_idxs=intervention_data.intervention_idxs,
            intervention_values=intervention_data.intervention_values,
        )
        # Evaluate log-prob per dimension
        intervention_log_probs = intervention_log_probs / (
            intervention_data.test_data.shape[1] - len(intervention_data.intervention_idxs)
        )

        all_log_probs.append(intervention_log_probs)

    per_intervention_log_probs_mean = [log_probs.mean(axis=0) for log_probs in all_log_probs]
    per_intervention_log_probs_std = [log_probs.std(axis=0) for log_probs in all_log_probs]

    if len(all_log_probs) > 0:
        all_log_probs_arr = np.concatenate(all_log_probs, axis=0)
    else:
        all_log_probs_arr = np.array([np.nan])

    return TreatmentDataLogProb(
        all_mean=all_log_probs_arr.mean(axis=0),
        all_std=all_log_probs_arr.std(axis=0),
        per_intervention_mean=per_intervention_log_probs_mean,
        per_intervention_std=per_intervention_log_probs_std,
    )


def get_ate_rms(
    model: IModelForInterventions,
    test_samples: np.ndarray,
    intervention_datasets: List[InterventionData],
    variables: Variables,
    most_likely_graph: bool = False,
    processed: bool = True,
) -> Tuple[AteRMSEMetrics, AteRMSEMetrics]:
    """
    Computes the rmse between the ground truth ate and the ate predicted by our model across all available interventions
        for both normalised and unnormalised data.
    Args:
        model: IModelForInterventions from which we can sample points while applying interventions
        test_samples: np.ndarray of shape (Nsamples, observation_dimension) containing samples from the non-intervened distribution p(y)
        intervention_datasets: List[InterventionData] containing intervetions and samples from the ground truth data generating process when the intervention is applied
        variables: Instance of Variables containing metadata used for normalisation
        most_likely_graph: whether to use the most likely causal graph (True) or to sample graphs (False)
        processed: whether the data has been processed
    Returns:
        Root mean square errors for both normalised and unnormalised data
    """

    group_rmses = []
    norm_group_rmses = []

    for intervention_data in intervention_datasets:

        if intervention_data.reference_data is not None:
            reference_data = intervention_data.reference_data
        else:
            reference_data = test_samples

        # conditions are applied to the test data when it is generated. As a result computing ATE on this data returns the CATE.
        ate = get_ate_from_samples(
            intervention_data.test_data, reference_data, variables, normalise=False, processed=processed
        )
        norm_ate = get_ate_from_samples(
            intervention_data.test_data, reference_data, variables, normalise=True, processed=processed
        )

        # Filter effect groups
        if intervention_data.effect_idxs is not None:
            [ate, norm_ate], filtered_variables = filter_effect_columns(
                [ate, norm_ate], variables, intervention_data.effect_idxs, processed
            )

        else:
            filtered_variables = variables

        # Check for conditioning
        if intervention_data.conditioning_idxs is not None:
            if most_likely_graph:
                Ngraphs = 1
                Nsamples_per_graph = 50000
            else:
                Ngraphs = 10
                Nsamples_per_graph = 5000
        else:
            if most_likely_graph:
                Ngraphs = 1
                Nsamples_per_graph = 20000
            else:
                Ngraphs = 10000
                Nsamples_per_graph = 2

        assert intervention_data.intervention_values is not None
        model_ate, model_norm_ate = model.cate(
            intervention_idxs=intervention_data.intervention_idxs,
            intervention_values=intervention_data.intervention_values,
            reference_values=intervention_data.intervention_reference,
            effect_idxs=intervention_data.effect_idxs,
            conditioning_idxs=intervention_data.conditioning_idxs,
            conditioning_values=intervention_data.conditioning_values,
            most_likely_graph=most_likely_graph,
            Nsamples_per_graph=Nsamples_per_graph,
            Ngraphs=Ngraphs,
        )

        group_rmses.append(calculate_per_group_rmse(model_ate, ate, filtered_variables))
        norm_group_rmses.append(calculate_per_group_rmse(model_norm_ate, norm_ate, filtered_variables))

    return AteRMSEMetrics(np.stack(group_rmses, axis=0)), AteRMSEMetrics(np.stack(norm_group_rmses, axis=0))


def get_ate_from_samples(
    intervened_samples: np.ndarray,
    baseline_samples: np.ndarray,
    variables: Variables,
    normalise: bool = False,
    processed: bool = True,
):
    """
    Computes ATE E[y | do(x)=a] - E[y] from samples of y from p(y | do(x)=a) and p(y)

    Args:
        intervened_samples: np.ndarray of shape (Nsamples, observation_dimension) containing samples from the intervened distribution p(y | do(x)=a)
        baseline_samples: np.ndarray of shape (Nsamples, observation_dimension) containing samples from the non-intervened distribution p(y)
        variables: Instance of Variables containing metada used for normalisation
        normalise: boolean indicating whether to normalise samples by their maximum and minimum values
        processed: whether the data has been processed (which affects the column numbering)
    """
    if normalise:
        assert variables is not None, "must provide an associated Variables instance to perform normalisation"
        intervened_samples, baseline_samples = normalise_data(
            [intervened_samples, baseline_samples], variables, processed
        )

    intervened_mean = intervened_samples.mean(axis=0)
    baseline_mean = baseline_samples.mean(axis=0)

    return intervened_mean - baseline_mean


def get_cate_from_samples(
    intervened_samples: torch.Tensor,
    baseline_samples: torch.Tensor,
    conditioning_mask: torch.Tensor,
    conditioning_values: torch.Tensor,
    effect_mask: torch.Tensor,
    variables: Variables,
    normalise: bool = False,
    rff_lengthscale: Union[int, float, List[float], Tuple[float, ...]] = (0.1, 1),
    rff_n_features: int = 3000,
):
    """
    Estimate CATE using a functional approach: We fit a function that takes as input the conditioning variables
     and as output the outcome variables using intervened_samples as training points. We do the same while using baseline_samples
     as training data. We estimate CATE as the difference between the functions' outputs when the input is set to conditioning_values.
     As functions we use linear models on a random fourier feature basis. If intervened_samples and baseline_samples are provided for multiple graphs
     the CATE estimate is averaged across graphs.

    Args:
        intervened_samples: tensor of shape (Ngraphs, Nsamples, Nvariables) sampled from intervened (non-conditional) distribution
        baseline_samples: tensor of shape (Ngraphs, Nsamples, Nvariables) sampled from a reference distribution. Note that this could mean a reference intervention has been applied.
        conditioning_mask: boolean tensor which indicates which variables we want to condition on
        conditioning_values: tensor containing values of variables we want to condition on
        effect_mask: boolean tensor which indicates which outcome variables for which we want to estimate CATE
        variables: Instance of Variables containing metada used for normalisation
        normalise: boolean indicating whether to normalise samples by their maximum and minimum values
        rff_lengthscale: either a positive float/int indicating the lengthscale of the RBF kernel or a list/tuple
         containing the lower and upper limits of a uniform distribution over the lengthscale. The latter option is prefereable when there is no prior knowledge about functional form.
        rff_n_features: Number of random features with which to approximate the RBF kernel. Larger numbers result in lower variance but are more computationally demanding.
    Returns:
        CATE_estimates: tensor of shape (len(effect_idxs)) containing our estimates of CATE for outcome variables
    """

    # TODO: we are assuming the conditioning variable is d-connected to the target but we should probably use the networkx dseparation method to check this in the future
    if normalise:
        assert variables is not None, "must provide an associated Variables instance to perform normalisation"
        intervened_samples_np, baseline_samples_np = normalise_data(
            [intervened_samples.cpu().numpy(), baseline_samples.cpu().numpy()], variables, processed=True
        )

        # Convert back to tensors
        intervened_samples = torch.tensor(
            intervened_samples_np, device=intervened_samples.device, dtype=intervened_samples.dtype
        )
        baseline_samples = torch.tensor(
            baseline_samples_np, device=baseline_samples.device, dtype=baseline_samples.dtype
        )

    assert effect_mask.sum() == 1.0, "Only 1d outcomes are supported"

    test_inputs = conditioning_values.unsqueeze(1)

    featuriser = MultiROFFeaturiser(rff_n_features=rff_n_features, lengthscale=rff_lengthscale)
    featuriser.fit(X=intervened_samples.new_ones((1, int(conditioning_mask.sum()))))

    CATE_estimates = []
    for graph_idx in range(intervened_samples.shape[0]):
        intervened_train_inputs = intervened_samples[graph_idx, :, conditioning_mask]
        reference_train_inputs = baseline_samples[graph_idx, :, conditioning_mask]

        featurised_intervened_train_inputs = featuriser.transform(intervened_train_inputs)
        featurised_reference_train_inputs = featuriser.transform(reference_train_inputs)
        featurised_test_input = featuriser.transform(test_inputs)

        intervened_train_targets = intervened_samples[graph_idx, :, effect_mask].reshape(intervened_samples.shape[1])
        reference_train_targets = baseline_samples[graph_idx, :, effect_mask].reshape(intervened_samples.shape[1])

        intervened_predictive_model = LinearModel()
        intervened_predictive_model.fit(features=featurised_intervened_train_inputs, targets=intervened_train_targets)

        reference_predictive_model = LinearModel()
        reference_predictive_model.fit(features=featurised_reference_train_inputs, targets=reference_train_targets)

        CATE_estimates.append(
            intervened_predictive_model.predict(features=featurised_test_input)[0]
            - reference_predictive_model.predict(features=featurised_test_input)[0]
        )

    return torch.stack(CATE_estimates, dim=0).mean(dim=0)


def get_ite_from_samples(
    intervention_samples: np.ndarray,
    reference_samples: np.ndarray,
    variables: Optional[Variables] = None,
    normalise: bool = False,
    processed: bool = True,
):
    """
    Calculates individual treatment effect (ITE) between two sets of samples each
    with shape (no. of samples, no. of variables).

    Args:
        intervention_samples (ndarray): Samples from intervened graph with shape (no. of samples, no. of dimenions).
        reference_samples (ndarray): Reference samples from intervened graph with shape (no. of samples, no. of dimenions).
        variables (Variables): A `Variables` instance relating to passed samples.
        normalised (bool): Flag indicating whether the data should be normalised (using `variables`) prior to
            calculating ITE.
        processed (bool): Flag indicating whether the passed samples have been processed.

    Returns: ITE with shape (no. of samples, no. of variables)
    """
    if normalise:
        assert variables is not None, "must provide an associated Variables instance to perform normalisation"
        intervention_samples, reference_samples = normalise_data(
            [intervention_samples, reference_samples], variables, processed
        )

    assert (
        intervention_samples.shape == reference_samples.shape
    ), "Intervention and reference samples must be the shape for ITE calculation"
    return intervention_samples - reference_samples


def calculate_rmse(a: np.ndarray, b: np.ndarray, axis: Optional[int] = None) -> np.ndarray:
    """
    Calculates the root mean squared error (RMSE) between arrays `a` and `b`.

    Args:
        a (ndarray): Array used for error calculation
        b (ndarray): Array used for error calculation
        axis (int): Axis upon which to calculate mean

    Returns: (ndarray) RMSE value taken along axis `axis`.
    """
    return np.sqrt(np.mean(np.square(np.subtract(a, b)), axis=axis))


def normalise_data(arrs: List[np.ndarray], variables: Variables, processed: bool) -> List[np.ndarray]:
    """
    Normalises all arrays in `arrs` to [0, 1] given variable maximums (upper) and minimums (lower)
    in `variables`. Categorical data is excluded from normalization.

    Args:
        arrs (List[ndarray]): A list of ndarrays to normalise
        variables (Variables): A Variables instance containing metadata about arrays in `arrs`
        processed (bool): Whether the data in `arrs` has been processed

    Returns:
        (list(ndarray)) A list of normalised ndarrays corresponding with `arrs`.
    """

    if processed:
        n_cols = variables.num_processed_cols
        col_groups = variables.processed_cols
    else:
        n_cols = variables.num_unprocessed_cols
        col_groups = variables.unprocessed_cols

    assert all(
        n_cols == arr.shape[-1] for arr in arrs
    ), f"Expected {n_cols} columns for the passed {'' if processed else 'non-'}processed array"

    # if lower/uppers aren't updated, performs (arr - 0)/(1 - 0), i.e. doesn't normalize
    lowers = np.zeros(n_cols)
    uppers = np.ones(n_cols)

    for cols_idx, variable in zip(col_groups, variables):
        if variable.type_ == "continuous":
            lowers[cols_idx] = variable.lower
            uppers[cols_idx] = variable.upper

    return [np.divide(np.subtract(arr, lowers), np.subtract(uppers, lowers)) for arr in arrs]


def calculate_per_group_rmse(a: np.ndarray, b: np.ndarray, variables: Variables) -> np.ndarray:
    """
    Calculates RMSE group-wise between two ndarrays (`a` and `b`) for all samples.
    Arrays 'a' and 'b' have expected shape (no. of rows, no. of variables) or (no. of variables).

    Args:
        a (ndarray): Array of shape (no. of rows, no. of variables)
        b (ndarray): Array of shape (no. of rows, no. of variables)
        variables (Variables): A Variables object indicating groups

    Returns:
        (ndarrray) RMSE calculated over each group for each sample in `a`/`b`
    """
    rmse_array = np.zeros((a.shape[0], variables.num_groups) if len(a.shape) == 2 else (variables.num_groups))
    for return_array_idx, group_idxs in enumerate(variables.group_idxs):
        # calculate RMSE columnwise for all samples
        rmse_array[..., return_array_idx] = calculate_rmse(a[..., group_idxs], b[..., group_idxs], axis=-1)
    return rmse_array


def filter_effect_columns(
    arrs: List[np.ndarray], variables: Variables, effect_idxs: np.ndarray, processed: bool
) -> Tuple[List[np.ndarray], Variables]:
    """
    Returns the columns associated with effect variables. If `proccessed` is True, assume
    that arrs has been processed and handle expanded columns appropriately.

    Args:
        arrs (List[ndarray]): A list of ndarrays to be filtered
        variables (Variables): A Variables instance containing metadata
        effect_idxs (np.ndarray): An array containing idxs of effect variables
        processed (bool): Whether to treat data in `arrs` as having been processed

    Returns: A list of ndarrays corresponding to `arrs` with columns relating to effect variables,
        and a new Variables instance relating to effect variables
    """
    if processed:
        # Get effect idxs according to processed data
        processed_effect_idxs = []
        for i in effect_idxs:
            processed_effect_idxs.extend(variables.processed_cols[i])
    else:
        processed_effect_idxs = effect_idxs.tolist()

    return [a[..., processed_effect_idxs] for a in arrs], variables.subset(effect_idxs.tolist())


def get_ite_evaluation_results(
    model: IModelForInterventions,
    counterfactual_datasets: List[InterventionData],
    variables: Variables,
    processed: bool,
    most_likely_graph: bool = False,
    Ngraphs: int = 100,
) -> Tuple[IteRMSEMetrics, IteRMSEMetrics]:
    """
    Calculates ITE evaluation metrics. Only evaluates target variables indicated in `variables`,
    if no target variables are indicate then evaluates all variables.

    Args:
        model (IModelForinterventions): Trained DECI model
        counterfactual_datasets (list[InterventionData]): a list of counterfactual datasets
            used to calculate metrics.
        variables (Variables): Variables object indicating variable group membership
        normalise (bool): Whether the data should be normalised prior to calculating RMSE
        processed (bool): Whether the data in `counterfactual_datasets` has been processed
        most_likely_graph (bool): Flag indicating whether to use most likely graph.
            If false, model-generated counterfactual samples are averaged over `Ngraph` graphs.
        Ngraphs (int): Number of graphs sampled when generating counterfactual samples. Unused if
            `most_likely_graph` is true.

    Returns:
            IteEvaluationResults object containing ITE evaluation metrics.
    """

    group_rmses = []
    norm_group_rmses = []
    for counterfactual_int_data in counterfactual_datasets:
        baseline_samples = counterfactual_int_data.conditioning_values
        reference_samples = counterfactual_int_data.reference_data
        intervention_samples = counterfactual_int_data.test_data
        assert intervention_samples is not None
        assert reference_samples is not None

        # get sample (ground truth) ite
        sample_ite = get_ite_from_samples(
            intervention_samples=intervention_samples,
            reference_samples=reference_samples,
            variables=variables,
            normalise=False,
            processed=processed,
        )

        sample_norm_ite = get_ite_from_samples(
            intervention_samples=intervention_samples,
            reference_samples=reference_samples,
            variables=variables,
            normalise=True,
            processed=processed,
        )
        assert isinstance(model, IModelForCounterfactuals)
        assert counterfactual_int_data.intervention_values is not None
        assert baseline_samples is not None
        # get model (predicted) ite
        model_ite, model_norm_ite = model.ite(
            X=baseline_samples,
            intervention_idxs=counterfactual_int_data.intervention_idxs,
            intervention_values=counterfactual_int_data.intervention_values,
            reference_values=counterfactual_int_data.intervention_reference,
            most_likely_graph=most_likely_graph,
            Ngraphs=Ngraphs,
        )

        # if there are defined target variables, only use these for evaluation
        if counterfactual_int_data.effect_idxs:
            arrs = [sample_ite, model_ite, sample_norm_ite, model_norm_ite]
            filtered_arrs, filtered_variables = filter_effect_columns(
                arrs, variables, counterfactual_int_data.effect_idxs, processed
            )
            [sample_ite, model_ite, sample_norm_ite, model_norm_ite] = filtered_arrs
        else:
            filtered_variables = variables

        # calculate ite rmse per group for current intervention
        # (no. of samples, no. of input variables) -> (no. of samples, no. of groups)
        group_rmses.append(calculate_per_group_rmse(sample_ite, model_ite, filtered_variables))
        norm_group_rmses.append(calculate_per_group_rmse(sample_norm_ite, model_norm_ite, filtered_variables))

    return IteRMSEMetrics(np.stack(group_rmses, axis=0)), IteRMSEMetrics(np.stack(norm_group_rmses, axis=0))


def calculate_regret(variables: Variables, X: torch.Tensor, target_idx: int, max_values: torch.Tensor) -> torch.Tensor:
    """Computes the regret function, given an array of maximum values.

    The regret is defined as

        regret(X) = max_values(X) - observed_outcome

    where `max_values(X)` is the maximum attainable value at `X`, which should be provided.
    This can be computed either with `posterior_expected_optimal_policy`, or by a user-defined method.

    Args:
        X: tensor of shape (num_samples, processed_dim_all) containing the contexts and observed outcomes
        target_idx: index of the target (outcome) variable. Should be 0 <= target_idx < num_nodes.
        max_values: tensor of shape (num_samples) containing the maximum value for each context. The ordering of rows
            should match with `X`.

    Returns:
        regret_values: tensor of shape (num_samples) containing the regret.
    """
    target_mask = get_mask_from_idxs([target_idx], group_mask=variables.group_mask, device=X.device)
    observed_values = X[..., target_mask].squeeze(-1)
    return max_values - observed_values


def dag_pen_np(X):
    assert X.shape[0] == X.shape[1]
    X = torch.from_numpy(X)
    return (torch.trace(torch.matrix_exp(X)) - X.shape[0]).item()


def int2binlist(i: int, n_bits: int):
    """
    Convert integer to list of ints with values in {0, 1}
    """
    assert i < 2**n_bits
    str_list = list(np.binary_repr(i, n_bits))
    return [int(i) for i in str_list]


def approximate_maximal_acyclic_subgraph(adj_matrix: np.ndarray, n_samples: int = 10):
    """
    Compute an (approximate) maximal acyclic subgraph of a directed non-dag but removing at most 1/2 of the edges
    See Vazirani, Vijay V. Approximation algorithms. Vol. 1. Berlin: springer, 2001, Page 7;
    Also Hassin, Refael, and Shlomi Rubinstein. "Approximations for the maximum acyclic subgraph problem."
    Information processing letters 51.3 (1994): 133-140.
    Args:
        adj_matrix: adjacency matrix of a directed graph (may contain cycles)
        n_samples: number of the random permutations generated. Default is 10.
    Returns:
        an adjacency matrix of the acyclic subgraph
    """
    # assign each node with a order
    adj_dag = np.zeros_like(adj_matrix)
    for _ in range(n_samples):
        random_order = np.expand_dims(np.random.permutation(adj_matrix.shape[0]), 0)
        # subgraph with only forward edges defined by the assigned order
        adj_forward = ((random_order.T > random_order).astype(int)) * adj_matrix
        # subgraph with only backward edges defined by the assigned order
        adj_backward = ((random_order.T < random_order).astype(int)) * adj_matrix
        # return the subgraph with the least deleted edges
        adj_dag_n = adj_forward if adj_backward.sum() < adj_forward.sum() else adj_backward
        if adj_dag_n.sum() > adj_dag.sum():
            adj_dag = adj_dag_n
    return adj_dag


def cpdag2dags(cp_mat: np.ndarray, samples: Optional[int] = None):
    """
    Compute all possible DAGs contained within a Markov equivalence class, given by a CPDAG
    Args:
        cp_mat: adjacency matrix containing both forward and backward edges for edges for which directionality is undetermined
    Returns:
        3 dimensional tensor, where the first indexes all the possible DAGs
    """
    assert len(cp_mat.shape) == 2 and cp_mat.shape[0] == cp_mat.shape[1]

    # matrix composed of just undetermined edges
    cycle_mat = (cp_mat == cp_mat.T) * cp_mat
    # return original matrix if there are no length-1 cycles
    if cycle_mat.sum() == 0:
        if dag_pen_np(cp_mat) != 0.0:
            cp_mat = approximate_maximal_acyclic_subgraph(cp_mat)
        return cp_mat[None, :, :]

    # matrix of determined edges
    cp_determined_subgraph = cp_mat - cycle_mat

    # prune cycles if the matrix of determined edges is not a dag
    if dag_pen_np(cp_determined_subgraph.copy()) != 0.0:
        cp_determined_subgraph = approximate_maximal_acyclic_subgraph(cp_determined_subgraph, 1000)

    # number of parent nodes for each node under the well determined matrix
    N_in_nodes = cp_determined_subgraph.sum(axis=0)

    # lower triangular version of cycles edges: only keep cycles in one direction.
    cycles_tril = np.tril(cycle_mat, k=-1)

    # indices of potential new edges
    undetermined_idx_mat = np.array(np.nonzero(cycles_tril)).T  # (N_undedetermined, 2)

    # number of undetermined edges
    N_undetermined = int(cycles_tril.sum())

    # choose random order for mask iteration
    max_dags = 2**N_undetermined
    if samples is None:
        samples = max_dags
    mask_indices = list(np.random.permutation(np.arange(max_dags)))

    # iterate over list of all potential combinations of new edges. 0 represents keeping edge from upper triangular and 1 from lower triangular
    dag_list: list = []
    while mask_indices and len(dag_list) < samples:

        mask_index = mask_indices.pop()
        mask = np.array(int2binlist(mask_index, N_undetermined))

        # extract list of indices which our new edges are pointing into
        incoming_edges = np.take_along_axis(undetermined_idx_mat, mask[:, None], axis=1).squeeze()

        # check if multiple edges are pointing at same node
        _, unique_counts = np.unique(incoming_edges, return_index=False, return_inverse=False, return_counts=True)

        # check if new colider has been created by checkig if multiple edges point at same node or if new edge points at existing child node
        new_colider = np.any(unique_counts > 1) or np.any(N_in_nodes[incoming_edges] > 0)

        if not new_colider:
            # get indices of new edges by sampling from lower triangular mat and upper triangular according to indices
            edge_selection = undetermined_idx_mat.copy()
            edge_selection[mask == 0, :] = np.fliplr(edge_selection[mask == 0, :])

            # add new edges to matrix and add to dag list
            new_dag = cp_determined_subgraph.copy()
            new_dag[(edge_selection[:, 0], edge_selection[:, 1])] = 1

            # Check for high order cycles
            if dag_pen_np(new_dag.copy()) == 0.0:
                dag_list.append(new_dag)
    # When all combinations of new edges create cycles, we will only keep determined ones
    if len(dag_list) == 0:
        dag_list.append(cp_determined_subgraph)

    return np.stack(dag_list, axis=0)


def process_adjacency_mats(adj_mats: np.ndarray, num_nodes: int):
    """
    This processes the adjacency matrix in the format [num, variable, variable]. It will remove the duplicates and non DAG adjacency matrix.
    Args:
        adj_mats (np.ndarry): A group of adjacency matrix
        num_nodes (int): The number of variables (dimensions of the adjacency matrix)

    Returns:
        A list of adjacency matrix without duplicates and non DAG
        A np.ndarray storing the weights of each adjacency matrix.
    """

    # This method will get rid of the non DAG and duplicated ones. It also returns a proper weight for each of the adjacency matrix
    if len(adj_mats.shape) == 2:
        # Single adjacency matrix
        assert (np.trace(scipy.linalg.expm(adj_mats)) - num_nodes) == 0, "Generate non DAG graph"
        return adj_mats, np.ones(1)
    else:
        # Multiple adjacency matrix samples
        # Remove non DAG adjacency matrix
        adj_mats = np.array(
            [adj_mat for adj_mat in adj_mats if (np.trace(scipy.linalg.expm(adj_mat)) - num_nodes) == 0]
        )
        assert np.any(adj_mats), "Generate non DAG graph"
        # Remove duplicated adjacency and aggregate the weights
        adj_mats_unique, dup_counts = np.unique(adj_mats, axis=0, return_counts=True)
        # Normalize the weights
        adj_weights = dup_counts / np.sum(dup_counts)
        return adj_mats_unique, adj_weights


def get_ipw_estimated_ate(
    interventional_X: Union[torch.Tensor, np.ndarray],
    treatment_probability: Union[torch.Tensor, np.ndarray],
    treatment_mask: Union[torch.Tensor, np.ndarray],
    outcome_idxs: Union[torch.Tensor, np.ndarray],
) -> np.ndarray:
    """Calculate the inverse probability weighted estimated ATE of the interventional data gather from
    either real-world experiments, or simulators.

    This is given by equation (6) of the write-up, which is defined as
    hat{ATE}: = sum_i 1/p_i * T_i Y_i/N_data  -  sum_i 1/(1-p_i) * (1-T_i) Y_i/N_data.

    Args:
        interventional_X: the tensor of shape (num_observations, input_dim) containing the
        interventional/counterfactual observations that are gathered from the real environment/simulator,
        after applying assigned treatments.
        treatment_probability: the tensor of  shape (num_obserbations,) containing the probability (induced by
        test policy) that each subject (row) of interventional X is assigned with treatment combinations at
        intervention values.
            hence 1-treatment_probability will be the probability that each subject (row) of interventional X is
            assigned with treatment combinations at reference values.
            Note that the actual treatment for each subject will be sampled and consolidated in this function.
        treatment_mask:  tensor of binary elements of shape (num_observations,) cotaining the simulated
           treatment assighment for each observation. treatment_mask_i = 1 indicates that subjet i is assigned
           with treatment of value intervention_values; otherwise reference_values is assigned.
        outcome_idxs: torch Tensor of shape (num_targets) containing indices of variables that specifies the outcome Y

    Returns:
        ndarray of shape (num_targets,) containing the ipw estimated value of ATE.
    """
    raise NotImplementedError


def get_real_world_testing_assignment(
    observational_X: Union[torch.Tensor, np.ndarray],
    intervention_idxs: Union[torch.Tensor, np.ndarray],
    intervention_values: Union[torch.Tensor, np.ndarray],
) -> Tuple[torch.Tensor, torch.Tensor]:
    """Calculate the treatment assigment probability for each subjects.
    This is defined as
    p(T_1=intervention_values_1, T_2=interventional_values_2, ..., |X).

    This can be implemented in two ways:
     1), A simple probability distribution given by certain rules. e.g., random A/B testing
     2), More complicated ones that implicitly depend on another deci model. In this case it should take as input a
     model instance.

    Args:
        observational_X: the tensor of shape (num_observations, input_dim) containing the observational data
        gathered from the real environment/simulator. Note that there aren't any interventions applied yet.
        intervention_idxs: torch.Tensor of shape (num_interventions) containing indices of variables that have
        been intervened.
        intervention_values: torch.Tensor of shape (num_interventons) optional array containing values for
        variables that have been intervened.
    Returns:
        Tuple (treatment_probability, treatment_mask).
           treatment_probability is a tensor of shape (num_observations,) containing the treatment_probability
           for each observation.
           treatment_mask is a tensor of binary elements of shape (num_observations,) cotaining the simulated
           treatment assighment for each observation. treatment_mask_i = 1 indicates that subjet i is assigned
           with treatment of value intervention_values; otherwise reference_values is assigned.

    """
    raise NotImplementedError


def eval_test_quality_by_ate_error(
    test_environment: IModelForCounterfactuals,
    observational_X: Union[torch.Tensor, np.ndarray],
    treatment_probability: Union[torch.Tensor, np.ndarray],
    treatment_mask: Union[torch.Tensor, np.ndarray],
    outcome_idxs: Union[torch.Tensor, np.ndarray],
    intervention_idxs: Union[torch.Tensor, np.ndarray],
    intervention_values: Union[torch.Tensor, np.ndarray],
    reference_values: Union[torch.Tensor, np.ndarray],
    Ngraphs: int = 1,
    most_likely_graph: bool = False,
) -> np.ndarray:
    """Calculate the quality of real-world testing based on ATE errors between golden standard (AB testing),
    and estimated ATE via treatment assignment policy.

    This is given by equation (7) of the write-up: https://www.overleaf.com/project/626be97093681b20faf29775,
    defined as ATE(do(T)): = E_{graph}E_{Y} [ Y | do(T=intervention_values)]
    - E_{graph}E_{Y}[ Y | do(T=reference_values)]

    This should be calculated using a separate instance of deci object that represents the simulator.

    Args:
        observational_X: the tensor of shape (num_observations, input_dim) containing the observational data
        gathered from the real environment/simulator. Note that there aren't any interventions applied yet.
        treatment_probability: the tensor of  shape (num_obserbations,) containing the probability (induced by
        test policy) that each subject (row) of interventional X is assigned with treatment combinations at
        intervention values.
            hence 1-treatment_probability will be the probability that each subject (row) of interventional X is
            assigned with treatment combinations at reference values.
            Note that the actual treatment for each subject will be sampled and consolidated in this function.
        treatment_mask is a tensor of binary elements of shape (num_observations,) cotaining the simulated
        treatment assighment for each observation.
            treatment_mask_i = 1 indicates that subjet i is assigned with treatment of value intervention_values;
            otherwise reference_values is assigned.
        outcome_idxs: torch Tensor of shape (num_targets) containing indices of variables that specifies the
        outcome Y
        intervention_idxs: torch.Tensor of shape (num_interventions) containing indices of variables that have
        been intervened.
            note that when num_interventions >1, the objective will be calculated as ATE(do(T_1,T_2,...))
        intervention_values: torch.Tensor of shape (num_interventions) optional array containing values for
        variables that have been intervened.
        reference_values: torch.Tensor containing a reference value for the treatment.
        Ngraphs: int containing number of graph samples to draw.
        most_likely_graph: bool indicating whether to deterministically pick the most probable graph under the
        approximate posterior instead of sampling graphs

    Returns:
        ndarray of shape (num_targets,) containing the estimating error for each targets
    """
    ground_truth_value = (
        test_environment.ite(
            X=observational_X,
            intervention_idxs=intervention_idxs,
            intervention_values=intervention_values,
            reference_values=reference_values,
            Ngraphs=Ngraphs,
            most_likely_graph=most_likely_graph,
        )
    )[:, outcome_idxs].mean(axis=0)
    interventional_X = torch.empty_like(torch.tensor(observational_X))
    interventional_X[treatment_mask == 1, :], _ = test_environment.ite(
        observational_X[treatment_mask == 1, :], intervention_idxs, intervention_values, Ngraphs=Ngraphs
    )
    interventional_X[treatment_mask == 0, :], _ = test_environment.ite(
        observational_X[treatment_mask == 0, :], intervention_idxs, reference_values, Ngraphs=Ngraphs
    )
    interventional_X += observational_X
    ipw_estimated_value = get_ipw_estimated_ate(interventional_X, treatment_probability, treatment_mask, outcome_idxs)

    return (ground_truth_value - ipw_estimated_value) ** 2
