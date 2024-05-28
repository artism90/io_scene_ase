from typing import Iterable

from bpy.types import Object, Context

from .ase import *
import bpy
import bmesh
import math
from mathutils import Matrix, Vector

SMOOTHING_GROUP_MAX = 32

class ASEBuilderError(Exception):
    pass


class ASEBuilderOptions(object):
    def __init__(self):
        self.scale = 1.0
        self.use_raw_mesh_data = False


class ASEBuilder(object):
    def build(self, context: Context, options: ASEBuilderOptions, objects: Iterable[Object]):
        ase = ASE()

        main_geometry_object = None

        mesh_objects = [obj for obj in objects if obj.type == 'MESH']
        context.window_manager.progress_begin(0, len(mesh_objects))

        for object_index, selected_object in enumerate(mesh_objects):
            # Evaluate the mesh after modifiers are applied
            if options.use_raw_mesh_data:
                mesh_object = selected_object
                mesh_data = mesh_object.data
            else:
                depsgraph = context.evaluated_depsgraph_get()
                bm = bmesh.new()
                bm.from_object(selected_object, depsgraph)
                mesh_data = bpy.data.meshes.new('')
                bm.to_mesh(mesh_data)
                del bm
                mesh_object = bpy.data.objects.new('', mesh_data)
                mesh_object.matrix_world = selected_object.matrix_world

            if not is_collision_name(selected_object.name) and main_geometry_object is not None:
                geometry_object = main_geometry_object
            else:
                geometry_object = ASEGeometryObject()
                geometry_object.name = selected_object.name
                if not geometry_object.is_collision:
                    main_geometry_object = geometry_object
                ase.geometry_objects.append(geometry_object)

            if geometry_object.is_collision:
                # Test that collision meshes are manifold and convex.
                bm = bmesh.new()
                bm.from_mesh(mesh_object.data)
                for edge in bm.edges:
                    if not edge.is_manifold:
                        del bm
                        raise ASEBuilderError(f'Collision mesh \'{selected_object.name}\' is not manifold')
                    if not edge.is_convex:
                        del bm
                        raise ASEBuilderError(f'Collision mesh \'{selected_object.name}\' is not convex')

            if not geometry_object.is_collision and len(selected_object.data.materials) == 0:
                raise ASEBuilderError(f'Mesh \'{selected_object.name}\' must have at least one material')

            vertex_transform = Matrix.Scale(options.scale, 4) @ Matrix.Rotation(math.pi, 4, 'Z') @ mesh_object.matrix_world

            for vertex_index, vertex in enumerate(mesh_data.vertices):
                geometry_object.vertices.append(vertex_transform @ vertex.co)

            material_indices = []
            if not geometry_object.is_collision:
                for mesh_material_index, material in enumerate(selected_object.data.materials):
                    if material is None:
                        raise ASEBuilderError(f'Material slot {mesh_material_index + 1} for mesh \'{selected_object.name}\' cannot be empty')
                    try:
                        # Reuse existing material entries for duplicates
                        material_index = ase.materials.index(material.name)
                    except ValueError:
                        material_index = len(ase.materials)
                        ase.materials.append(material.name)
                    material_indices.append(material_index)

            mesh_data.calc_loop_triangles()

            # Calculate smoothing groups.
            poly_groups, groups = mesh_data.calc_smooth_groups(use_bitflags=False)

            # Figure out how many scaling axes are negative.
            # This is important for calculating the normals of the mesh.
            _, _, scale = vertex_transform.decompose()
            negative_scaling_axes = sum([1 for x in scale if x < 0])
            should_invert_normals = negative_scaling_axes % 2 == 1

            loop_triangle_index_order = (2, 1, 0) if should_invert_normals else (0, 1, 2)

            # Faces
            for face_index, loop_triangle in enumerate(mesh_data.loop_triangles):
                face = ASEFace()
                face.a, face.b, face.c = map(lambda j: geometry_object.vertex_offset + mesh_data.loops[loop_triangle.loops[j]].vertex_index, loop_triangle_index_order)
                if not geometry_object.is_collision:
                    face.material_index = material_indices[loop_triangle.material_index]
                # The UT2K4 importer only accepts 32 smoothing groups. Anything past this completely mangles the
                # smoothing groups and effectively makes the whole model use sharp-edge rendering.
                # The fix is to constrain the smoothing group between 0 and 31 by applying a modulo of 32 to the actual
                # smoothing group index.
                # This may result in bad calculated normals on export in rare cases. For example, if a face with a
                # smoothing group of 3 is adjacent to a face with a smoothing group of 35 (35 % 32 == 3), those faces
                # will be treated as part of the same smoothing group.
                face.smoothing = (poly_groups[loop_triangle.polygon_index] - 1) % SMOOTHING_GROUP_MAX
                geometry_object.faces.append(face)

            if not geometry_object.is_collision:
                # Normals
                for face_index, loop_triangle in enumerate(mesh_data.loop_triangles):
                    face_normal = ASEFaceNormal()
                    face_normal.normal = loop_triangle.normal
                    face_normal.vertex_normals = []
                    for i in loop_triangle_index_order:
                        vertex_normal = ASEVertexNormal()
                        vertex_normal.vertex_index = geometry_object.vertex_offset + mesh_data.loops[loop_triangle.loops[i]].vertex_index
                        vertex_normal.normal = loop_triangle.split_normals[i]
                        if should_invert_normals:
                            vertex_normal.normal = (-Vector(vertex_normal.normal)).to_tuple()
                        face_normal.vertex_normals.append(vertex_normal)
                    geometry_object.face_normals.append(face_normal)

                # Texture Coordinates
                for i, uv_layer_data in enumerate([x.data for x in mesh_data.uv_layers]):
                    if i >= len(geometry_object.uv_layers):
                        geometry_object.uv_layers.append(ASEUVLayer())
                    uv_layer = geometry_object.uv_layers[i]
                    for loop_index, loop in enumerate(mesh_data.loops):
                        u, v = uv_layer_data[loop_index].uv
                        uv_layer.texture_vertices.append((u, v, 0.0))

                # Texture Faces
                for loop_triangle in mesh_data.loop_triangles:
                    geometry_object.texture_vertex_faces.append(
                        tuple(map(lambda l: geometry_object.texture_vertex_offset + loop_triangle.loops[l], loop_triangle_index_order))
                    )

                # Vertex Colors
                if len(mesh_data.vertex_colors) > 0:
                    if mesh_data.vertex_colors.active is not None:
                        vertex_colors = mesh_data.vertex_colors.active.data
                        for color in map(lambda x: x.color, vertex_colors):
                            geometry_object.vertex_colors.append(tuple(color[0:3]))

            # Update data offsets for next iteration
            geometry_object.texture_vertex_offset += len(mesh_data.loops)
            geometry_object.vertex_offset = len(geometry_object.vertices)

            context.window_manager.progress_update(object_index)

        context.window_manager.progress_end()

        if len(ase.geometry_objects) == 0:
            raise ASEBuilderError('At least one mesh object must be selected')

        if main_geometry_object is None:
            raise ASEBuilderError('At least one non-collision mesh must be exported')

        return ase
