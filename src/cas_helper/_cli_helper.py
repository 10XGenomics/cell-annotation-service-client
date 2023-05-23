import scanpy as sc
import numpy as np
import pandas as pd
import scipy.sparse as sp

import warnings
from collections import defaultdict
from operator import itemgetter
from typing import Optional

from tqdm.notebook import tqdm


def validate_adata_for_cas(
    adata: sc.AnnData,
    casp_feature_list_csv_path: str,
    int_count_matrix: str = "X",
    gene_symbols_column_name: str = "__index__",
    gene_ids_column_name: str = "gene_ids",
    missing_features_policy: str = "replace_with_zero",
    extra_features_policy: str = "ignore",
) -> sc.AnnData:

    # we only have the following policies implemented
    assert missing_features_policy == "replace_with_zero"
    assert extra_features_policy == "ignore"

    # where to find the integer count matrix?
    assert int_count_matrix in {"raw", "X"}

    # fetch gene_symbols
    if gene_symbols_column_name == "__index__":
        adata_gene_symbols = adata.var.index.tolist()
    else:
        adata_gene_symbols = adata.var[gene_symbols_column_name].values.tolist()

    # fetch gene_ids
    if gene_ids_column_name == "__index__":
        adata_gene_ids = adata.var.index.tolist()
    else:
        adata_gene_ids = adata.var[gene_ids_column_name].values.tolist()

    # reformat anndata object for CAS
    casp_gene_id_list = pd.read_csv(casp_feature_list_csv_path, index_col=0)[
        "gene_id"
    ].values.tolist()
    adata_gene_id_list = adata_gene_ids
    gene_id_to_gene_symbol_map = {
        gene_id: gene_symbol
        for gene_symbol, gene_id in zip(adata_gene_symbols, adata_gene_ids)
    }
    casp_gene_symbol_list = [
        gene_id_to_gene_symbol_map[gene_id]
        if gene_id in gene_id_to_gene_symbol_map
        else "N/A"
        for gene_id in casp_gene_id_list
    ]

    gene_id_intersection = list(set(adata_gene_id_list).intersection(casp_gene_id_list))
    casp_gene_id_map = {
        gene_id: index for index, gene_id in enumerate(casp_gene_id_list)
    }
    adata_gene_id_map = {
        gene_id: index for index, gene_id in enumerate(adata_gene_id_list)
    }
    gene_id_intersection_casp_indices = list(
        map(casp_gene_id_map.get, gene_id_intersection)
    )
    gene_id_intersection_adata_indices = list(
        map(adata_gene_id_map.get, gene_id_intersection)
    )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", sp.SparseEfficiencyWarning)
        n_cells = adata.shape[0]
        n_genes = len(casp_gene_id_list)
        X = sp.csc_matrix((n_cells, n_genes), dtype=np.float32)
        adata_X = adata.X if int_count_matrix == "X" else adata.raw.X
        X[:, gene_id_intersection_casp_indices] = adata_X.tocsc()[
            :, gene_id_intersection_adata_indices
        ]

        # workaround
        obs = adata.obs.copy()
        obs.index = pd.Index([str(x) for x in np.arange(adata.shape[0])])

        adata = sc.AnnData(
            X.tocsr(),
            obs=obs,
            obsm=adata.obsm,
            var=pd.DataFrame(
                data={"gene_symbols": casp_gene_symbol_list}, index=casp_gene_id_list
            ),
        )

    return adata


def reduce_cas_query_result_by_majority_vote(
    adata: sc.AnnData,
    cas_query_res: dict,
    output_cell_type_key: str = "cas_cell_type",
    output_cell_type_confidence_score_key: str = "cas_cell_type_confidence_score",
):

    majority_vote_cell_type_list = []
    majority_vote_confidence_score_list = []
    for single_cell_query in cas_query_res:
        total_cell_count = 0
        best_cell_count = -1
        best_cell_type = None
        for match in single_cell_query["matches"]:
            total_cell_count += match["cell_count"]
            if match["cell_count"] > best_cell_count:
                best_cell_count = match["cell_count"]
                best_cell_type = match["cell_type"]
        majority_vote_cell_type_list.append(best_cell_type)
        majority_vote_confidence_score_list.append(best_cell_count / total_cell_count)

    adata.obs[output_cell_type_key] = majority_vote_cell_type_list
    adata.obs[
        output_cell_type_confidence_score_key
    ] = majority_vote_confidence_score_list


def reduce_cas_query_result_by_min_distance(
    adata: sc.AnnData,
    cas_query_res: dict,
    output_cell_type_key: str = "cas_cell_type",
    output_cell_type_min_distance_key: str = "cas_cell_type_min_distance",
):

    min_distance_cell_type_list = []
    min_distance_list = []
    for single_cell_query in cas_query_res:
        min_distance = np.inf
        best_cell_type = None
        for match in single_cell_query["matches"]:
            if match["min_distance"] < min_distance:
                min_distance = match["min_distance"]
                best_cell_type = match["cell_type"]
        min_distance_cell_type_list.append(best_cell_type)
        min_distance_list.append(min_distance)

    adata.obs[output_cell_type_key] = min_distance_cell_type_list
    adata.obs[output_cell_type_min_distance_key] = min_distance_list


def reduce_cas_query_result_by_majority_vote_per_cluster(
    adata: sc.AnnData,
    cas_query_res: dict,
    cluster_key: str = "leiden",
    output_cell_type_key: str = "cas_per_cluster_cell_type",
    output_cell_type_confidence_score_key: str = "cas_per_cluster_cell_type_confidence_score",
    ignore_set: set = set(),
) -> dict:

    assert adata.shape[0] == len(cas_query_res)
    assert cluster_key in adata.obs

    output_cell_type_list = [None] * adata.shape[0]
    output_cell_type_confidence_score_list = [None] * adata.shape[0]
    cluster_detailed_info_dict = dict()

    for cluster_id in adata.obs[cluster_key].values.categories:

        # obtain cell indices belonging to cluster_id
        cluster_cell_indices = np.where(adata.obs[cluster_key] == cluster_id)[0]
        assert len(cluster_cell_indices) > 0

        # summarize the hits of all cells
        cluster_query = defaultdict(int)
        for cell_index in cluster_cell_indices:
            single_cell_query = cas_query_res[cell_index]
            for match in single_cell_query["matches"]:
                if match["cell_type"] in ignore_set:
                    continue
                cluster_query[match["cell_type"]] += match["cell_count"]

        # identify best cell type for the cluster
        total_cell_count = sum(cluster_query.values())
        sorted_hits_and_freqs = sorted(
            [
                (cell_type, count / total_cell_count)
                for cell_type, count in cluster_query.items()
            ],
            key=itemgetter(1),
            reverse=True,
        )
        best_cell_type = sorted_hits_and_freqs[0][0]
        best_cell_type_confidence_score = sorted_hits_and_freqs[0][1]

        # bookkeeping
        cluster_detailed_info_dict[cluster_id] = sorted_hits_and_freqs
        for cell_index in cluster_cell_indices:
            output_cell_type_list[cell_index] = best_cell_type
            output_cell_type_confidence_score_list[
                cell_index
            ] = best_cell_type_confidence_score

    adata.obs[output_cell_type_key] = output_cell_type_list
    adata.obs[
        output_cell_type_confidence_score_key
    ] = output_cell_type_confidence_score_list

    return cluster_detailed_info_dict


def reduce_cas_query_result_by_wnn(
    adata: sc.AnnData,
    cas_query_res: dict,
    n_neighbors: int = 10,
    wnn_strategy: str = "connectivities",
    connectivities_key: str = "connectivities",
    self_connectivity: float = 1.0,
    min_n_cells_per_type: int = 10,
    output_unreliable_type: str = "Unknown or Unconfident",
    output_cell_type_key: str = "cas_cell_type",
    output_cell_type_confidence_score_key: str = "cas_cell_type_confidence_score",
):

    assert wnn_strategy in {"connectivities"}
    assert n_neighbors <= adata.uns["neighbors"]["params"]["n_neighbors"]
    assert len(adata) == len(cas_query_res)

    all_cell_types = []
    for single_cell_query in cas_query_res:
        for match in single_cell_query["matches"]:
            all_cell_types.append(match["cell_type"])
    all_cell_types = list(set(all_cell_types))
    cell_type_to_idx_map = {
        cell_type: idx for idx, cell_type in enumerate(all_cell_types)
    }

    def _get_weights_via_fuzzy_simplicial_sets(
        i, adata, n_neighbors, self_connectivity
    ) -> tuple:
        connectivity_values = adata.obsp[connectivities_key][i].data
        connectivity_indices = adata.obsp[connectivities_key][i].indices

        # append self
        connectivity_values = np.append(connectivity_values, self_connectivity)
        connectivity_indices = np.append(connectivity_indices, i)

        # convert to weights
        _order = np.argsort(connectivity_values)[::-1][:n_neighbors]
        sorted_connectivity_values = connectivity_values[_order]
        sorted_connectivity_indices = connectivity_indices[_order]
        weights = sorted_connectivity_values / np.sum(sorted_connectivity_values)
        return sorted_connectivity_indices, weights

    def _get_cell_type_probs(
        single_cell_query, all_cell_types, cell_type_to_idx_map
    ) -> np.ndarray:
        probs = np.zeros((len(all_cell_types),))
        for match in single_cell_query["matches"]:
            probs[cell_type_to_idx_map[match["cell_type"]]] += match["cell_count"]
        return probs / np.sum(probs)

    cell_type_probs_list = []
    majority_vote_cell_type_list = []
    majority_vote_confidence_score_list = []

    n_cells = len(cas_query_res)
    for i in tqdm(range(n_cells)):

        neighbor_indices, neighbor_weights = _get_weights_via_fuzzy_simplicial_sets(
            i, adata, n_neighbors, self_connectivity
        )
        assert np.isclose(np.sum(neighbor_weights), 1.0)

        cell_type_probs = np.zeros((len(all_cell_types),))
        for j, weight in zip(neighbor_indices, neighbor_weights):
            cell_type_probs += weight * _get_cell_type_probs(
                cas_query_res[j], all_cell_types, cell_type_to_idx_map
            )

        best_cell_type_idx = np.argmax(cell_type_probs)
        best_cell_type = all_cell_types[best_cell_type_idx]
        best_cell_type_prob = cell_type_probs[best_cell_type_idx]

        cell_type_probs_list.append(cell_type_probs)
        majority_vote_cell_type_list.append(best_cell_type)
        majority_vote_confidence_score_list.append(best_cell_type_prob)

    all_cell_types = set(majority_vote_cell_type_list)
    update_map = dict()
    for cell_type in all_cell_types:
        n_counts = sum(
            [_cell_type == cell_type for _cell_type in majority_vote_cell_type_list]
        )
        if n_counts < min_n_cells_per_type:
            update_map[cell_type] = output_unreliable_type
        else:
            update_map[cell_type] = cell_type
    majority_vote_cell_type_list = list(
        map(update_map.get, majority_vote_cell_type_list)
    )

    adata.obs[output_cell_type_key] = majority_vote_cell_type_list
    adata.obs[
        output_cell_type_confidence_score_key
    ] = majority_vote_confidence_score_list

    return cell_type_to_idx_map, cell_type_probs_list


def hex_to_rgb(value: str) -> np.ndarray:
    value = value.lstrip("#")
    lv = len(value)
    return np.asarray(
        tuple(float(int(value[i : i + lv // 3], 16)) for i in range(0, lv, lv // 3))
    )


def rgb_to_tuple(value: np.ndarray) -> tuple:
    return (int(value[0]), int(value[1]), int(value[2]))


def rgb_to_hex(rgb: np.ndarray) -> str:
    return "#%02x%02x%02x" % rgb


def get_interpolated_cell_type_colors(
    adata,
    cell_type_to_idx_map,
    cell_type_probs_list,
    na_cell_type_key="Unknown or Unconfident",
) -> np.ndarray:
    idx_to_cell_type_map = {
        idx: cell_type for cell_type, idx in cell_type_to_idx_map.items()
    }
    all_cell_types = list(
        map(idx_to_cell_type_map.get, range(len(cell_type_to_idx_map)))
    )
    plot_cell_type_colors = adata.uns["cas_cell_type_colors"]
    plot_cell_types = list(adata.obs["cas_cell_type"].values.categories)
    plot_cell_types_to_idx_map = {
        cell_type: idx for idx, cell_type in enumerate(plot_cell_types)
    }

    cell_type_probs_nk = np.asarray(cell_type_probs_list)
    cell_type_map_kq = np.zeros((len(all_cell_types), len(plot_cell_types)))
    na_index = plot_cell_types.index(na_cell_type_key)
    plot_cell_type_colors[na_index] = rgb_to_hex((255, 255, 255))
    for k, input_cell_type in enumerate(all_cell_types):
        if input_cell_type in plot_cell_types_to_idx_map:
            q = plot_cell_types_to_idx_map[input_cell_type]
        else:
            q = na_index
        cell_type_map_kq[k, q] += 1

    plot_colors_q3 = np.asarray(
        list(hex_to_rgb(hex_color) for hex_color in plot_cell_type_colors)
    )
    cell_colors_n3 = (cell_type_probs_nk @ cell_type_map_kq @ plot_colors_q3) / 255

    return np.clip(cell_colors_n3, 0.0, 1.0)
