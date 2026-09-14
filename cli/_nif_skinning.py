"""NIF skinning tool functions for CLI use. Operate on NifFile objects directly."""
from __future__ import annotations
import os
from typing import Any

import numpy as np

from creation_lib.nif.nif_file import NifFile, NifBlock


def _error(msg: str) -> dict:
    return {"error": msg}


def _extract_skin_data_from_nif(nif: NifFile, shape_block: NifBlock):
    """Extract SkinData from a BSTriShape block within a loaded NifFile."""
    from creation_lib.skinning.skin_data import SkinData
    from creation_lib.skinning.reference_body import _get_skin_bone_names

    vertex_data_list = shape_block.get_field("Vertex Data") or []
    triangles_list = shape_block.get_field("Triangles") or []

    if not vertex_data_list:
        raise ValueError(f"Shape block {shape_block.block_id} has no Vertex Data")

    n_verts = len(vertex_data_list)

    positions = np.zeros((n_verts, 3), dtype=np.float32)
    normals = np.zeros((n_verts, 3), dtype=np.float32)
    uvs = np.zeros((n_verts, 2), dtype=np.float32)
    weights = np.zeros((n_verts, 4), dtype=np.float32)
    bone_idx_arr = np.zeros((n_verts, 4), dtype=np.int32)

    for i, vd in enumerate(vertex_data_list):
        v = vd.get("Vertex") or {}
        positions[i] = [
            float(v.get("x", 0)),
            float(v.get("y", 0)),
            float(v.get("z", 0)),
        ]

        n = vd.get("Normal")
        if n:
            normals[i] = [
                float(n.get("x", 0)),
                float(n.get("y", 0)),
                float(n.get("z", 0)),
            ]

        uv = vd.get("UV")
        if uv:
            uvs[i] = [float(uv.get("u", 0)), float(uv.get("v", 0))]

        bw_list = vd.get("Bone Weights") or vd.get("BoneWeights") or []
        if isinstance(bw_list, list):
            for j, bw in enumerate(bw_list[:4]):
                if isinstance(bw, dict):
                    bone_idx_arr[i, j] = int(bw.get("index", bw.get("Index", 0)))
                    weights[i, j] = float(bw.get("weight", bw.get("Weight", 0)))

    bone_names = _get_skin_bone_names(nif, shape_block)

    # Extract triangles
    tris: list[list[int]] = []
    for tri in triangles_list:
        if isinstance(tri, dict):
            tris.append([
                int(tri.get("v1", tri.get("V1", 0))),
                int(tri.get("v2", tri.get("V2", 0))),
                int(tri.get("v3", tri.get("V3", 0))),
            ])
        elif isinstance(tri, (list, tuple)) and len(tri) >= 3:
            tris.append([int(tri[0]), int(tri[1]), int(tri[2])])

    tri_arr = np.array(tris, dtype=np.uint32) if tris else np.empty((0, 3), dtype=np.uint32)

    return SkinData(
        vertices=positions,
        triangles=tri_arr,
        normals=normals,
        uvs=uvs,
        bone_names=bone_names,
        weights=weights,
        bone_indices=bone_idx_arr,
        partitions=np.full(len(tri_arr), -1, dtype=np.int32),
        max_bones_per_vertex=4,
    )


def _write_skin_data_back(nif: NifFile, shape_block: NifBlock, skin_data) -> None:
    """Write SkinData weights back into the NIF's vertex data bone weight fields."""
    vertex_data_list = shape_block.get_field("Vertex Data") or []
    n_verts = min(len(vertex_data_list), skin_data.num_vertices)

    for i in range(n_verts):
        bw_entries = []
        for j in range(skin_data.weights.shape[1]):
            w = float(skin_data.weights[i, j])
            bi = int(skin_data.bone_indices[i, j])
            bw_entries.append({"index": bi, "weight": w})
        vertex_data_list[i]["Bone Weights"] = bw_entries

    shape_block.set_field("Vertex Data", vertex_data_list)


def _find_shape_block(nif: NifFile, shape_id: int) -> NifBlock | dict:
    """Find a BSTriShape block by ID, returning error dict if not found."""
    block = nif.get_block(shape_id)
    if block is None:
        return _error(f"Block {shape_id} not found")
    schema = nif.schema
    if not schema.is_subtype_of(block.type_name, "BSTriShape"):
        return _error(
            f"Block {shape_id} is '{block.type_name}', expected BSTriShape or subtype"
        )
    return block


def auto_skin(
    nif: NifFile,
    shape_id: int = 0,
    reference_path: str = "",
    method: str = "hybrid",
    game: str = "fo4",
    gender: str = "female",
) -> dict:
    """Automatically skin a mesh shape using reference body weights."""
    try:
        from creation_lib.skinning import (
            transfer_weights as lib_transfer_weights,
            normalize_weights as lib_normalize_weights,
            assign_partitions_from_bones,
            load_reference_body,
            extract_skin_data_from_nif,
        )
        from creation_lib.skinning.skin_data import SkinData

        block = _find_shape_block(nif, shape_id)
        if isinstance(block, dict):
            return block

        target_sd = _extract_skin_data_from_nif(nif, block)

        if reference_path:
            ref_sd = extract_skin_data_from_nif(reference_path)
        else:
            env_key = f"{game.upper()}_EXTRACTED_DIR"
            extracted_dir = os.environ.get(env_key, "")
            if not extracted_dir:
                env_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "..", ".env"
                )
                if os.path.isfile(env_path):
                    with open(env_path) as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith(env_key + "="):
                                extracted_dir = line.split("=", 1)[1].strip().strip('"').strip("'")
                                break
            if not extracted_dir:
                return _error(
                    f"No reference_path provided and {env_key} not set in environment or .env"
                )
            ref_sd = load_reference_body(
                extracted_dir, game=game, gender=gender
            )

        weights, bone_indices, stats = lib_transfer_weights(
            ref_sd, target_sd.vertices, target_sd.triangles, method=method,
        )

        weights, bone_indices, norm_count = lib_normalize_weights(
            weights, bone_indices, max_bones=4
        )

        skinned_sd = SkinData(
            vertices=target_sd.vertices,
            triangles=target_sd.triangles,
            normals=target_sd.normals,
            uvs=target_sd.uvs,
            bone_names=ref_sd.bone_names,
            weights=weights,
            bone_indices=bone_indices,
            partitions=np.full(target_sd.num_triangles, -1, dtype=np.int32),
            max_bones_per_vertex=4,
        )

        partitions = assign_partitions_from_bones(skinned_sd)
        skinned_sd.partitions = partitions

        _write_skin_data_back(nif, block, skinned_sd)

        unique, counts = np.unique(partitions, return_counts=True)
        partition_summary = {int(p): int(c) for p, c in zip(unique, counts)}

        total_per_vert = weights.sum(axis=1)
        zero_weight_count = int((total_per_vert < 1e-6).sum())

        return {
            "vertices": int(target_sd.num_vertices),
            "triangles": int(target_sd.num_triangles),
            "bones_transferred": len(ref_sd.bone_names),
            "transfer_stats": stats,
            "normalized_vertices": norm_count,
            "zero_weight_vertices": zero_weight_count,
            "partition_summary": partition_summary,
        }
    except Exception as e:
        return _error(str(e))


def transfer_weights(
    src_nif: NifFile,
    source_shape_id: int,
    tgt_nif: NifFile,
    target_shape_id: int,
    method: str = "hybrid",
    search_radius: float = 10.0,
) -> dict:
    """Transfer bone weights from one shape to another."""
    try:
        from creation_lib.skinning import transfer_weights as lib_transfer_weights
        from creation_lib.skinning.skin_data import SkinData

        src_block = _find_shape_block(src_nif, source_shape_id)
        if isinstance(src_block, dict):
            return src_block
        tgt_block = _find_shape_block(tgt_nif, target_shape_id)
        if isinstance(tgt_block, dict):
            return tgt_block

        source_sd = _extract_skin_data_from_nif(src_nif, src_block)
        target_sd = _extract_skin_data_from_nif(tgt_nif, tgt_block)

        weights, bone_indices, stats = lib_transfer_weights(
            source_sd, target_sd.vertices, target_sd.triangles,
            method=method, search_radius=search_radius,
        )

        result_sd = SkinData(
            vertices=target_sd.vertices,
            triangles=target_sd.triangles,
            normals=target_sd.normals,
            uvs=target_sd.uvs,
            bone_names=source_sd.bone_names,
            weights=weights,
            bone_indices=bone_indices,
            partitions=target_sd.partitions,
            max_bones_per_vertex=4,
        )
        _write_skin_data_back(tgt_nif, tgt_block, result_sd)

        bone_stats = {}
        for bi in range(len(source_sd.bone_names)):
            mask = bone_indices == bi
            bone_weights = weights[mask]
            if len(bone_weights) > 0:
                bone_stats[source_sd.bone_names[bi]] = {
                    "vertex_count": int((bone_weights > 0).sum()),
                    "avg_weight": float(bone_weights[bone_weights > 0].mean()) if (bone_weights > 0).any() else 0.0,
                }

        return {
            "transfer_stats": stats,
            "target_vertices": int(target_sd.num_vertices),
            "bone_stats": bone_stats,
        }
    except Exception as e:
        return _error(str(e))


def generate_partitions(
    nif: NifFile,
    shape_id: int = 0,
    reference_path: str = "",
    method: str = "from_bones",
) -> dict:
    """Generate dismemberment partition assignments for a skinned shape."""
    try:
        from creation_lib.skinning import (
            assign_partitions_from_bones,
            assign_partitions_from_reference,
            extract_skin_data_from_nif,
        )

        block = _find_shape_block(nif, shape_id)
        if isinstance(block, dict):
            return block

        skin_data = _extract_skin_data_from_nif(nif, block)

        if method == "from_reference":
            if not reference_path:
                return _error("reference_path is required for 'from_reference' method")
            ref_sd = extract_skin_data_from_nif(reference_path)
            partitions = assign_partitions_from_reference(skin_data, ref_sd)
        else:
            partitions = assign_partitions_from_bones(skin_data)

        unique, counts = np.unique(partitions, return_counts=True)
        partition_map = {int(p): int(c) for p, c in zip(unique, counts)}

        return {
            "total_triangles": int(skin_data.num_triangles),
            "partition_map": partition_map,
            "method": method,
        }
    except Exception as e:
        return _error(str(e))


def validate_weights(
    nif: NifFile,
    shape_id: int = 0,
) -> dict:
    """Validate bone weights on a skinned shape."""
    try:
        block = _find_shape_block(nif, shape_id)
        if isinstance(block, dict):
            return block

        skin_data = _extract_skin_data_from_nif(nif, block)

        n_verts = skin_data.num_vertices
        issues: dict[str, Any] = {
            "total_vertices": n_verts,
            "total_triangles": skin_data.num_triangles,
            "total_bones": len(skin_data.bone_names),
        }

        weight_sums = skin_data.weights.sum(axis=1)
        unnormalized_mask = np.abs(weight_sums - 1.0) > 0.01
        issues["unnormalized_vertices"] = int(unnormalized_mask.sum())
        if issues["unnormalized_vertices"] > 0:
            worst_indices = np.where(unnormalized_mask)[0][:10]
            issues["unnormalized_samples"] = [
                {"vertex": int(vi), "weight_sum": float(weight_sums[vi])}
                for vi in worst_indices
            ]

        zero_mask = weight_sums < 1e-6
        issues["zero_weight_vertices"] = int(zero_mask.sum())

        nonzero_counts = (skin_data.weights > 0).sum(axis=1)
        over_limit = nonzero_counts > 4
        issues["over_limit_vertices"] = int(over_limit.sum())

        unassigned = (skin_data.partitions < 0).sum() if skin_data.num_triangles > 0 else 0
        issues["unassigned_partitions"] = int(unassigned)

        issues["valid"] = (
            issues["unnormalized_vertices"] == 0
            and issues["zero_weight_vertices"] == 0
            and issues["over_limit_vertices"] == 0
        )

        return issues
    except Exception as e:
        return _error(str(e))


def normalize_weights(
    nif: NifFile,
    shape_id: int = 0,
    max_bones: int = 4,
) -> dict:
    """Normalize bone weights on a skinned shape."""
    try:
        from creation_lib.skinning import normalize_weights as lib_normalize_weights
        from creation_lib.skinning.skin_data import SkinData

        block = _find_shape_block(nif, shape_id)
        if isinstance(block, dict):
            return block

        skin_data = _extract_skin_data_from_nif(nif, block)

        weights, bone_indices, modified_count = lib_normalize_weights(
            skin_data.weights, skin_data.bone_indices, max_bones=max_bones
        )

        result_sd = SkinData(
            vertices=skin_data.vertices,
            triangles=skin_data.triangles,
            normals=skin_data.normals,
            uvs=skin_data.uvs,
            bone_names=skin_data.bone_names,
            weights=weights,
            bone_indices=bone_indices,
            partitions=skin_data.partitions,
            max_bones_per_vertex=max_bones,
        )
        _write_skin_data_back(nif, block, result_sd)

        return {
            "total_vertices": int(skin_data.num_vertices),
            "modified_vertices": modified_count,
            "max_bones": max_bones,
        }
    except Exception as e:
        return _error(str(e))
