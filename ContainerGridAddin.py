import adsk.core
import adsk.fusion
import traceback
import json
import os


APP_NAME = "Container Grid Generator"
CMD_ID = "containerGridGeneratorCmd"
CMD_NAME = "Create Compartment Container"
CMD_DESCRIPTION = "Generate a parameter-driven open-top compartment container."
WORKSPACE_ID = "FusionSolidEnvironment"
PANEL_ID = "SolidCreatePanel"

PALETTE_ID = "containerGridLayoutPalette"
PALETTE_NAME = "Compartment Layout Editor"

DEFAULTS = {
    "containerLength": 92.0,
    "containerHeight": 60.0,
    "containerDepth": 72.0,
    "wallThickness": 1.0,
    "rows": 2,
    "cols": 2,
    "bottomEdgeFilletWest": 3.0,
    "bottomEdgeFilletEast": 3.0,
    "bottomEdgeFilletSouth": 3.0,
    "bottomEdgeFilletNorth": 3.0,
}

_handlers = []
_active_palette = None
_active_command = None

_committed_layout_leaves = []
_pending_layout_leaves = None
_custom_layout_applied = False


def _to_mm(cm_value: float) -> float:
    return cm_value * 10.0


def _center_from_bbox(entity) -> adsk.core.Point3D:
    box = entity.boundingBox
    return adsk.core.Point3D.create(
        (box.minPoint.x + box.maxPoint.x) * 0.5,
        (box.minPoint.y + box.maxPoint.y) * 0.5,
        (box.minPoint.z + box.maxPoint.z) * 0.5,
    )


def _edge_midpoint(edge: adsk.fusion.BRepEdge) -> adsk.core.Point3D:
    evaluator = edge.evaluator
    ok_range, start_param, end_param = evaluator.getParameterExtents()
    if not ok_range:
        return _center_from_bbox(edge)
    mid_param = (start_param + end_param) * 0.5
    ok_point, point = evaluator.getPointAtParameter(mid_param)
    if not ok_point:
        return _center_from_bbox(edge)
    return point


def _line_direction(edge: adsk.fusion.BRepEdge):
    line = adsk.core.Line3D.cast(edge.geometry)
    if line:
        sp = line.startPoint
        ep = line.endPoint
        d = adsk.core.Vector3D.create(ep.x - sp.x, ep.y - sp.y, ep.z - sp.z)
        d.normalize()
        return d

    evaluator = edge.evaluator
    ok_range, start_param, end_param = evaluator.getParameterExtents()
    if not ok_range:
        return None
    ok_s, sp = evaluator.getPointAtParameter(start_param)
    ok_e, ep = evaluator.getPointAtParameter(end_param)
    if not ok_s or not ok_e:
        return None
    ok_m, mp = evaluator.getPointAtParameter((start_param + end_param) * 0.5)
    if ok_m:
        chord_mx = (sp.x + ep.x) * 0.5
        chord_my = (sp.y + ep.y) * 0.5
        chord_mz = (sp.z + ep.z) * 0.5
        deviation = ((mp.x - chord_mx) ** 2 + (mp.y - chord_my) ** 2 + (mp.z - chord_mz) ** 2) ** 0.5
        if deviation > 0.001:
            return None
    dx, dy, dz = ep.x - sp.x, ep.y - sp.y, ep.z - sp.z
    length = (dx * dx + dy * dy + dz * dz) ** 0.5
    if length < 1e-8:
        return None
    return adsk.core.Vector3D.create(dx / length, dy / length, dz / length)


def _new_leaf(leaf_id: int, x0: float, x1: float, z0: float, z1: float):
    return {"id": leaf_id, "x0": x0, "x1": x1, "z0": z0, "z1": z1}


def _seed_grid_leaves(rows: int, cols: int):
    leaves = []
    next_id = 1
    for r in range(rows):
        z0 = r / rows
        z1 = (r + 1) / rows
        for c in range(cols):
            x0 = c / cols
            x1 = (c + 1) / cols
            leaves.append(_new_leaf(next_id, x0, x1, z0, z1))
            next_id += 1
    return leaves


def _upsert_param(design, name, expression, units, comment):
    user_params = design.userParameters
    existing = user_params.itemByName(name)
    if existing:
        if existing.expression != expression:
            existing.expression = expression
        return existing
    return user_params.add(name, adsk.core.ValueInput.createByString(expression), units, comment)


def ensure_parameters(design, params):
    _upsert_param(design, "containerLength", f"{_to_mm(params['containerLength']):.3f} mm", "mm", "Container length on X axis.")
    _upsert_param(design, "containerHeight", f"{_to_mm(params['containerHeight']):.3f} mm", "mm", "Container height on Y axis.")
    _upsert_param(design, "containerDepth", f"{_to_mm(params['containerDepth']):.3f} mm", "mm", "Container depth on Z axis.")
    _upsert_param(design, "wallThickness", f"{_to_mm(params['wallThickness']):.3f} mm", "mm", "Shell and divider wall thickness.")
    _upsert_param(design, "rows", f"{int(params['rows'])}", "", "Compartment rows.")
    _upsert_param(design, "cols", f"{int(params['cols'])}", "", "Compartment columns.")
    _upsert_param(design, "bottomEdgeFilletWest", f"{_to_mm(params['bottomEdgeFilletWest']):.3f} mm", "mm", "Bottom west edge fillet for all compartments.")
    _upsert_param(design, "bottomEdgeFilletEast", f"{_to_mm(params['bottomEdgeFilletEast']):.3f} mm", "mm", "Bottom east edge fillet for all compartments.")
    _upsert_param(design, "bottomEdgeFilletSouth", f"{_to_mm(params['bottomEdgeFilletSouth']):.3f} mm", "mm", "Bottom south edge fillet for all compartments.")
    _upsert_param(design, "bottomEdgeFilletNorth", f"{_to_mm(params['bottomEdgeFilletNorth']):.3f} mm", "mm", "Bottom north edge fillet for all compartments.")


def _leaf_dimensions(params: dict, leaf: dict):
    wall = params["wallThickness"]
    interior_x = params["containerLength"] - (2 * wall)
    interior_z = params["containerDepth"] - (2 * wall)
    return (
        max(0.0, (leaf["x1"] - leaf["x0"]) * interior_x),
        max(0.0, (leaf["z1"] - leaf["z0"]) * interior_z),
    )


def _preview_leaves(params):
    global _pending_layout_leaves, _custom_layout_applied, _committed_layout_leaves
    if _pending_layout_leaves is not None:
        return _pending_layout_leaves
    if _custom_layout_applied and _committed_layout_leaves:
        return _committed_layout_leaves
    return _seed_grid_leaves(int(params["rows"]), int(params["cols"]))


def _execute_leaves(params):
    global _custom_layout_applied, _committed_layout_leaves
    if _custom_layout_applied and _committed_layout_leaves:
        return _committed_layout_leaves
    return _seed_grid_leaves(int(params["rows"]), int(params["cols"]))


def _validate_inputs(params, leaves):
    if params["containerLength"] <= 0 or params["containerHeight"] <= 0 or params["containerDepth"] <= 0:
        return "Container dimensions must be greater than 0."
    if params["rows"] < 1 or params["cols"] < 1:
        return "Rows and columns must be at least 1."
    if params["wallThickness"] <= 0:
        return "Wall thickness must be greater than 0."

    wall = params["wallThickness"]
    length = params["containerLength"]
    height = params["containerHeight"]
    depth = params["containerDepth"]
    if wall * 2 >= length or wall * 2 >= depth or wall >= height:
        return "Wall thickness is too large for the selected container dimensions."

    interior_x = length - (2 * wall)
    interior_z = depth - (2 * wall)
    if interior_x <= 0 or interior_z <= 0:
        return "Wall thickness leaves no interior space."

    if not leaves:
        return "Layout has no compartments."

    min_dim = 1e9
    for leaf in leaves:
        lx, lz = _leaf_dimensions(params, leaf)
        if lx <= 0 or lz <= 0:
            return "Layout contains invalid compartment sizes."
        min_dim = min(min_dim, lx, lz)

    max_bottom = min_dim * 0.5
    for side_key in ("bottomEdgeFilletWest", "bottomEdgeFilletEast", "bottomEdgeFilletSouth", "bottomEdgeFilletNorth"):
        r = float(params[side_key])
        if r < 0:
            return "All bottom-edge fillets must be 0 or greater."
        if r > max_bottom:
            return f"Bottom-edge fillet ({_to_mm(r):.1f} mm) is too large for the smallest compartment ({_to_mm(max_bottom):.1f} mm max)."
    return None


def _read_params_from_inputs(inputs):
    return {
        "containerLength": adsk.core.ValueCommandInput.cast(inputs.itemById("containerLength")).value,
        "containerHeight": adsk.core.ValueCommandInput.cast(inputs.itemById("containerHeight")).value,
        "containerDepth": adsk.core.ValueCommandInput.cast(inputs.itemById("containerDepth")).value,
        "wallThickness": adsk.core.ValueCommandInput.cast(inputs.itemById("wallThickness")).value,
        "rows": adsk.core.IntegerSpinnerCommandInput.cast(inputs.itemById("rows")).value,
        "cols": adsk.core.IntegerSpinnerCommandInput.cast(inputs.itemById("cols")).value,
        "bottomEdgeFilletWest": adsk.core.ValueCommandInput.cast(inputs.itemById("bottomEdgeFilletWest")).value,
        "bottomEdgeFilletEast": adsk.core.ValueCommandInput.cast(inputs.itemById("bottomEdgeFilletEast")).value,
        "bottomEdgeFilletSouth": adsk.core.ValueCommandInput.cast(inputs.itemById("bottomEdgeFilletSouth")).value,
        "bottomEdgeFilletNorth": adsk.core.ValueCommandInput.cast(inputs.itemById("bottomEdgeFilletNorth")).value,
    }


def _build_outer_shell(comp, params):
    length = params["containerLength"]
    height = params["containerHeight"]
    depth = params["containerDepth"]
    wall = params["wallThickness"]

    sketch = comp.sketches.add(comp.xZConstructionPlane)
    lines = sketch.sketchCurves.sketchLines
    center = adsk.core.Point3D.create(0, 0, 0)
    corner = adsk.core.Point3D.create(length * 0.5, depth * 0.5, 0)
    lines.addCenterPointRectangle(center, corner)

    profile = sketch.profiles.item(0)
    extrudes = comp.features.extrudeFeatures
    outer_extrude = extrudes.addSimple(profile, adsk.core.ValueInput.createByReal(height), adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    body = outer_extrude.bodies.item(0)

    top_face = None
    max_y = -1e9
    for face in body.faces:
        plane = adsk.core.Plane.cast(face.geometry)
        if not plane:
            continue
        normal = plane.normal
        normal.normalize()
        if normal.y < 0.999:
            continue
        y_val = _center_from_bbox(face).y
        if y_val > max_y:
            max_y = y_val
            top_face = face

    if not top_face:
        raise RuntimeError("Unable to identify top face for shell operation.")

    shell_faces = adsk.core.ObjectCollection.create()
    shell_faces.add(top_face)
    shell_features = comp.features.shellFeatures
    shell_input = shell_features.createInput(shell_faces, True)
    shell_input.insideThickness = adsk.core.ValueInput.createByReal(wall)
    shell_features.add(shell_input)
    return body


def _extrude_sketch_profiles(comp, sketch, height, body):
    if sketch.profiles.count == 0:
        return
    profiles = adsk.core.ObjectCollection.create()
    for idx in range(sketch.profiles.count):
        profiles.add(sketch.profiles.item(idx))

    extrudes = comp.features.extrudeFeatures
    ext_input = extrudes.createInput(profiles, adsk.fusion.FeatureOperations.JoinFeatureOperation)
    extent = adsk.fusion.DistanceExtentDefinition.create(adsk.core.ValueInput.createByReal(height))
    ext_input.setOneSideExtent(extent, adsk.fusion.ExtentDirections.PositiveExtentDirection)
    ext_input.participantBodies = [body]
    extrudes.add(ext_input)


def _merge_segments(segments):
    if not segments:
        return []
    segments = sorted(segments, key=lambda s: (s[0], s[1]))
    out = [list(segments[0])]
    eps = 1e-7
    for start, end in segments[1:]:
        last = out[-1]
        if start <= last[1] + eps:
            last[1] = max(last[1], end)
        else:
            out.append([start, end])
    return [(m[0], m[1]) for m in out]


def _internal_boundaries(leaves):
    v_map = {}
    h_map = {}
    eps = 1e-7

    for a in leaves:
        for b in leaves:
            if a["id"] == b["id"]:
                continue
            if abs(a["x1"] - b["x0"]) < eps:
                lo = max(a["z0"], b["z0"])
                hi = min(a["z1"], b["z1"])
                if hi - lo > eps:
                    v_map.setdefault(round(a["x1"], 6), []).append((lo, hi))
            if abs(a["z1"] - b["z0"]) < eps:
                lo = max(a["x0"], b["x0"])
                hi = min(a["x1"], b["x1"])
                if hi - lo > eps:
                    h_map.setdefault(round(a["z1"], 6), []).append((lo, hi))

    return ({k: _merge_segments(v) for k, v in v_map.items()}, {k: _merge_segments(v) for k, v in h_map.items()})


def _build_divider_walls(comp, body, params, leaves):
    if len(leaves) <= 1:
        return

    length = params["containerLength"]
    height = params["containerHeight"]
    depth = params["containerDepth"]
    wall = params["wallThickness"]

    x_min = -length * 0.5 + wall
    z_min = -depth * 0.5 + wall
    interior_x = (length - 2 * wall)
    interior_z = (depth - 2 * wall)

    v_boundaries, h_boundaries = _internal_boundaries(leaves)

    if v_boundaries:
        sketch_v = comp.sketches.add(comp.xZConstructionPlane)
        lines = sketch_v.sketchCurves.sketchLines
        for x_norm, segs in v_boundaries.items():
            x_center = x_min + (x_norm * interior_x)
            for z0n, z1n in segs:
                z0 = z_min + (z0n * interior_z)
                z1 = z_min + (z1n * interior_z)
                lines.addTwoPointRectangle(adsk.core.Point3D.create(x_center - wall * 0.5, z0, 0), adsk.core.Point3D.create(x_center + wall * 0.5, z1, 0))
        _extrude_sketch_profiles(comp, sketch_v, height, body)

    if h_boundaries:
        sketch_h = comp.sketches.add(comp.xZConstructionPlane)
        lines = sketch_h.sketchCurves.sketchLines
        for z_norm, segs in h_boundaries.items():
            z_center = z_min + (z_norm * interior_z)
            for x0n, x1n in segs:
                x0 = x_min + (x0n * interior_x)
                x1 = x_min + (x1n * interior_x)
                lines.addTwoPointRectangle(adsk.core.Point3D.create(x0, z_center - wall * 0.5, 0), adsk.core.Point3D.create(x1, z_center + wall * 0.5, 0))
        _extrude_sketch_profiles(comp, sketch_h, height, body)


def _compartment_interior_bounds(leaf, x_min, z_min, interior_x, interior_z, wall):
    eps = 1e-7
    half_wall = wall * 0.5
    nx0, nx1 = leaf["x0"], leaf["x1"]
    nz0, nz1 = leaf["z0"], leaf["z1"]

    actual_x0 = x_min + nx0 * interior_x + (half_wall if nx0 > eps else 0.0)
    actual_x1 = x_min + nx1 * interior_x - (half_wall if nx1 < 1.0 - eps else 0.0)
    actual_z0 = z_min + nz0 * interior_z + (half_wall if nz0 > eps else 0.0)
    actual_z1 = z_min + nz1 * interior_z - (half_wall if nz1 < 1.0 - eps else 0.0)

    return actual_x0, actual_x1, actual_z0, actual_z1


SIDE_PARAM_MAP = {
    "west": "bottomEdgeFilletWest",
    "east": "bottomEdgeFilletEast",
    "south": "bottomEdgeFilletSouth",
    "north": "bottomEdgeFilletNorth",
}


def _closest_delta(value: float, candidates: list) -> float:
    if not candidates:
        return 1e9
    return min(abs(value - c) for c in candidates)


def _collect_bottom_edge_groups(body, params, leaves):
    wall = params["wallThickness"]
    length = params["containerLength"]
    depth = params["containerDepth"]
    x_min = -length * 0.5 + wall
    z_min = -depth * 0.5 + wall
    interior_x = length - 2 * wall
    interior_z = depth - 2 * wall

    west_xs = set()
    east_xs = set()
    south_zs = set()
    north_zs = set()
    for leaf in leaves:
        ax0, ax1, az0, az1 = _compartment_interior_bounds(
            leaf, x_min, z_min, interior_x, interior_z, wall
        )
        west_xs.add(round(ax0, 8))
        east_xs.add(round(ax1, 8))
        south_zs.add(round(az0, 8))
        north_zs.add(round(az1, 8))

    pos_tol = max(0.01, wall * 0.6)
    axis_tol = max(0.005, wall * 0.4)
    y_tol = 0.05
    envelope = wall

    west_vals = sorted(west_xs)
    east_vals = sorted(east_xs)
    south_vals = sorted(south_zs)
    north_vals = sorted(north_zs)

    groups = {}
    for edge in body.edges:
        ev = edge.evaluator
        ok_range, p0, p1 = ev.getParameterExtents()
        if not ok_range:
            continue
        ok_s, sp = ev.getPointAtParameter(p0)
        ok_e, ep = ev.getPointAtParameter(p1)
        if not ok_s or not ok_e:
            continue
        ok_m, mp = ev.getPointAtParameter((p0 + p1) * 0.5)
        if not ok_m:
            continue

        if abs(mp.y - wall) > y_tol:
            continue
        if mp.x < x_min - envelope or mp.x > x_min + interior_x + envelope:
            continue
        if mp.z < z_min - envelope or mp.z > z_min + interior_z + envelope:
            continue

        dx = abs(ep.x - sp.x)
        dy = abs(ep.y - sp.y)
        dz = abs(ep.z - sp.z)

        if dy > max(dx, dz) * 0.1 + 0.001:
            continue

        side_name = None
        if abs(sp.z - ep.z) <= axis_tol and dx > axis_tol:
            edge_z = (sp.z + ep.z) * 0.5
            south_delta = _closest_delta(edge_z, south_vals)
            north_delta = _closest_delta(edge_z, north_vals)
            if min(south_delta, north_delta) <= pos_tol:
                side_name = "south" if south_delta <= north_delta else "north"
        elif abs(sp.x - ep.x) <= axis_tol and dz > axis_tol:
            edge_x = (sp.x + ep.x) * 0.5
            west_delta = _closest_delta(edge_x, west_vals)
            east_delta = _closest_delta(edge_x, east_vals)
            if min(west_delta, east_delta) <= pos_tol:
                side_name = "west" if west_delta <= east_delta else "east"

        if not side_name:
            continue

        param_key = SIDE_PARAM_MAP[side_name]
        radius = float(params[param_key])
        if radius <= 0:
            continue

        key = round(radius, 6)
        if key not in groups:
            groups[key] = adsk.core.ObjectCollection.create()
        groups[key].add(edge)

    return groups


def apply_internal_fillets(comp, body, params, leaves):
    groups = _collect_bottom_edge_groups(body, params, leaves)
    if not groups:
        return

    fillet_features = comp.features.filletFeatures
    fillet_input = fillet_features.createInput()

    for radius_key, edge_collection in groups.items():
        radius_cm = float(radius_key)
        if radius_cm <= 0 or edge_collection.count == 0:
            continue
        fillet_input.edgeSetInputs.addConstantRadiusEdgeSet(
            edge_collection,
            adsk.core.ValueInput.createByReal(radius_cm),
            False,
        )

    if fillet_input.edgeSetInputs.count > 0:
        fillet_features.add(fillet_input)


def build_container(design, params, leaves):
    comp = design.rootComponent
    body = _build_outer_shell(comp, params)
    body.name = APP_NAME
    _build_divider_walls(comp, body, params, leaves)
    apply_internal_fillets(comp, body, params, leaves)


def _request_preview_refresh():
    global _active_command
    if not _active_command:
        return
    try:
        _active_command.doExecutePreview()
    except Exception:
        pass


def _sync_layout_input_enabled_state():
    global _active_command, _custom_layout_applied
    if not _active_command:
        return
    try:
        inputs = _active_command.commandInputs
        rows_inp = adsk.core.IntegerSpinnerCommandInput.cast(inputs.itemById("rows"))
        cols_inp = adsk.core.IntegerSpinnerCommandInput.cast(inputs.itemById("cols"))
        if rows_inp:
            rows_inp.isEnabled = not _custom_layout_applied
        if cols_inp:
            cols_inp.isEnabled = not _custom_layout_applied
    except Exception:
        pass


def _send_layout_to_palette(palette, leaves):
    if not palette:
        return
    payload = {"type": "layout", "leaves": leaves}
    try:
        palette.sendInfoToHTML("layout", json.dumps(payload))
    except Exception:
        pass


def _open_custom_layout_editor(inputs):
    global _pending_layout_leaves, _active_palette
    ui = adsk.core.Application.get().userInterface
    rows = adsk.core.IntegerSpinnerCommandInput.cast(inputs.itemById("rows")).value
    cols = adsk.core.IntegerSpinnerCommandInput.cast(inputs.itemById("cols")).value
    _pending_layout_leaves = _seed_grid_leaves(int(rows), int(cols))
    palette = _ensure_palette(ui)
    palette.isVisible = True
    _active_palette = palette
    _send_layout_to_palette(palette, _pending_layout_leaves)
    _request_preview_refresh()


def _ensure_palette(ui):
    global _active_palette
    palette = ui.palettes.itemById(PALETTE_ID)
    if not palette:
        html_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "layout_editor.html")
        html_url = "file:///" + html_path.replace("\\", "/")
        palette = ui.palettes.add(PALETTE_ID, PALETTE_NAME, html_url, True, True, True, 420, 560)
        html_handler = PaletteHTMLHandler()
        palette.incomingFromHTML.add(html_handler)
        _handlers.append(html_handler)
    _active_palette = palette
    return palette


class PaletteHTMLHandler(adsk.core.HTMLEventHandler):
    def notify(self, args):
        global _pending_layout_leaves, _committed_layout_leaves, _custom_layout_applied, _active_palette
        html_args = adsk.core.HTMLEventArgs.cast(args)
        action = html_args.action

        try:
            payload = json.loads(html_args.data) if html_args.data else {}
        except Exception:
            payload = {}

        if action == "layoutChanged":
            leaves = payload.get("leaves", [])
            parsed = []
            for leaf in leaves:
                parsed.append(_new_leaf(
                    int(leaf["id"]),
                    float(leaf["x0"]), float(leaf["x1"]),
                    float(leaf["z0"]), float(leaf["z1"]),
                ))
            _pending_layout_leaves = parsed
            _request_preview_refresh()
            return

        if action == "done":
            if _pending_layout_leaves is not None:
                _committed_layout_leaves = list(_pending_layout_leaves)
                _custom_layout_applied = True
            _pending_layout_leaves = None
            _sync_layout_input_enabled_state()
            if _active_palette:
                _active_palette.isVisible = False
            _request_preview_refresh()
            return

        if action == "cancel":
            _pending_layout_leaves = None
            _sync_layout_input_enabled_state()
            if _active_palette:
                _active_palette.isVisible = False
            _request_preview_refresh()
            return


class CommandExecutePreviewHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            app = adsk.core.Application.get()
            event_args = adsk.core.CommandEventArgs.cast(args)
            command = event_args.firingEvent.sender
            params = _read_params_from_inputs(command.commandInputs)
            leaves = _preview_leaves(params)

            validation_error = _validate_inputs(params, leaves)
            if validation_error:
                return

            design = adsk.fusion.Design.cast(app.activeProduct)
            if not design:
                return

            try:
                build_container(design, params, leaves)
            except Exception:
                return
            event_args.isValidResult = True
        except Exception:
            pass


class CommandExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        ui = None
        try:
            app = adsk.core.Application.get()
            ui = app.userInterface
            event_args = adsk.core.CommandEventArgs.cast(args)
            command = event_args.firingEvent.sender
            params = _read_params_from_inputs(command.commandInputs)
            leaves = _execute_leaves(params)

            validation_error = _validate_inputs(params, leaves)
            if validation_error:
                ui.messageBox(validation_error)
                return

            design = adsk.fusion.Design.cast(app.activeProduct)
            if not design:
                ui.messageBox("No active Fusion design found.")
                return

            ensure_parameters(design, params)
            build_container(design, params, leaves)
        except Exception:
            if ui:
                ui.messageBox(f"Failed:\n{traceback.format_exc()}")


class CommandInputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        global _custom_layout_applied, _committed_layout_leaves, _pending_layout_leaves, _active_palette
        try:
            event_args = adsk.core.InputChangedEventArgs.cast(args)
            changed = event_args.input
            inputs = event_args.inputs

            if changed.id == "customLayout":
                btn = adsk.core.BoolValueCommandInput.cast(changed)
                if btn and btn.value:
                    _open_custom_layout_editor(inputs)
                    btn.value = False
                return

            if changed.id == "clearCustomLayout":
                btn = adsk.core.BoolValueCommandInput.cast(changed)
                if btn and btn.value:
                    _custom_layout_applied = False
                    _committed_layout_leaves = []
                    _sync_layout_input_enabled_state()
                    _request_preview_refresh()
                    btn.value = False
                return

            if changed.id == "resetDefaults":
                btn = adsk.core.BoolValueCommandInput.cast(changed)
                if btn and btn.value:
                    for dim_key in ("containerLength", "containerHeight", "containerDepth",
                                    "wallThickness", "bottomEdgeFilletWest", "bottomEdgeFilletEast",
                                    "bottomEdgeFilletSouth", "bottomEdgeFilletNorth"):
                        inp = adsk.core.ValueCommandInput.cast(inputs.itemById(dim_key))
                        if inp:
                            inp.expression = f"{DEFAULTS[dim_key]} mm"
                    adsk.core.IntegerSpinnerCommandInput.cast(inputs.itemById("rows")).value = int(DEFAULTS["rows"])
                    adsk.core.IntegerSpinnerCommandInput.cast(inputs.itemById("cols")).value = int(DEFAULTS["cols"])

                    _pending_layout_leaves = None
                    _custom_layout_applied = False
                    _committed_layout_leaves = []
                    if _active_palette:
                        _active_palette.isVisible = False
                    _sync_layout_input_enabled_state()
                    _request_preview_refresh()
                    btn.value = False
                return
        except Exception:
            pass


class CommandDestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        global _active_command, _pending_layout_leaves
        _active_command = None
        _pending_layout_leaves = None


class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        global _active_command
        ui = None
        try:
            app = adsk.core.Application.get()
            ui = app.userInterface
            event_args = adsk.core.CommandCreatedEventArgs.cast(args)
            cmd = event_args.command
            _active_command = cmd
            inputs = cmd.commandInputs

            default_len = adsk.core.ValueInput.createByString(f"{DEFAULTS['containerLength']} mm")
            default_height = adsk.core.ValueInput.createByString(f"{DEFAULTS['containerHeight']} mm")
            default_depth = adsk.core.ValueInput.createByString(f"{DEFAULTS['containerDepth']} mm")
            default_wall = adsk.core.ValueInput.createByString(f"{DEFAULTS['wallThickness']} mm")
            default_bottom_west = adsk.core.ValueInput.createByString(f"{DEFAULTS['bottomEdgeFilletWest']} mm")
            default_bottom_east = adsk.core.ValueInput.createByString(f"{DEFAULTS['bottomEdgeFilletEast']} mm")
            default_bottom_south = adsk.core.ValueInput.createByString(f"{DEFAULTS['bottomEdgeFilletSouth']} mm")
            default_bottom_north = adsk.core.ValueInput.createByString(f"{DEFAULTS['bottomEdgeFilletNorth']} mm")

            inputs.addValueInput("containerLength", "Length (X)", "mm", default_len)
            inputs.addValueInput("containerHeight", "Height (Y / Up)", "mm", default_height)
            inputs.addValueInput("containerDepth", "Depth (Z)", "mm", default_depth)
            inputs.addValueInput("wallThickness", "Wall Thickness", "mm", default_wall)
            inputs.addIntegerSpinnerCommandInput("rows", "Rows", 1, 100, 1, int(DEFAULTS["rows"]))
            inputs.addIntegerSpinnerCommandInput("cols", "Columns", 1, 100, 1, int(DEFAULTS["cols"]))

            fillet_group = inputs.addGroupCommandInput("filletsGroup", "Fillets")
            fillet_inputs = fillet_group.children
            fillet_inputs.addValueInput("bottomEdgeFilletWest", "Bottom West Edge", "mm", default_bottom_west)
            fillet_inputs.addValueInput("bottomEdgeFilletEast", "Bottom East Edge", "mm", default_bottom_east)
            fillet_inputs.addValueInput("bottomEdgeFilletSouth", "Bottom South Edge", "mm", default_bottom_south)
            fillet_inputs.addValueInput("bottomEdgeFilletNorth", "Bottom North Edge", "mm", default_bottom_north)

            inputs.addBoolValueInput("customLayout", "Custom Layout", False, "", False)
            inputs.addBoolValueInput("clearCustomLayout", "Clear Custom Layout", False, "", False)
            inputs.addBoolValueInput("resetDefaults", "Reset Defaults", False, "", False)
            on_execute = CommandExecuteHandler()
            cmd.execute.add(on_execute)
            _handlers.append(on_execute)

            on_preview = CommandExecutePreviewHandler()
            cmd.executePreview.add(on_preview)
            _handlers.append(on_preview)

            on_changed = CommandInputChangedHandler()
            cmd.inputChanged.add(on_changed)
            _handlers.append(on_changed)

            on_destroy = CommandDestroyHandler()
            cmd.destroy.add(on_destroy)
            _handlers.append(on_destroy)

            _sync_layout_input_enabled_state()

        except Exception:
            if ui:
                ui.messageBox(f"Failed:\n{traceback.format_exc()}")


def run(context):
    ui = None
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface

        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if not cmd_def:
            cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_DESCRIPTION)

        on_created = CommandCreatedHandler()
        cmd_def.commandCreated.add(on_created)
        _handlers.append(on_created)

        workspace = ui.workspaces.itemById(WORKSPACE_ID)
        panel = workspace.toolbarPanels.itemById(PANEL_ID)
        control = panel.controls.itemById(CMD_ID)
        if not control:
            panel.controls.addCommand(cmd_def)

        _ensure_palette(ui)
        _active_palette.isVisible = False
    except Exception:
        if ui:
            ui.messageBox(f"Failed:\n{traceback.format_exc()}")


def stop(context):
    ui = None
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface

        workspace = ui.workspaces.itemById(WORKSPACE_ID)
        panel = workspace.toolbarPanels.itemById(PANEL_ID)
        control = panel.controls.itemById(CMD_ID)
        if control:
            control.deleteMe()

        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()

        palette = ui.palettes.itemById(PALETTE_ID)
        if palette:
            palette.deleteMe()
    except Exception:
        if ui:
            ui.messageBox(f"Failed:\n{traceback.format_exc()}")
