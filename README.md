# Fusion 360 Container Grid Add-In

This add-in creates an open-top container centered on the origin with a bottom, parameterized wall thickness, compartment grid dividers, and internal fillets.

## Default Geometry

- Length (X): `92 mm`
- Height (Y / up): `60 mm`
- Depth (Z): `72 mm`
- Wall thickness: `2 mm`
- Rows: `3`
- Columns: `4`
- Bottom edge fillets: per-side controls for each compartment (`North`, `South`, `East`, `West`)

## Orientation

- Open side faces `+Y`
- Container base sits on the `X-Z` plane at `Y=0`
- Body is centered at origin in `X` and `Z`

## Install and Run

1. Open Fusion 360.
2. Go to **Utilities > Scripts and Add-Ins**.
3. Open the **Add-Ins** tab and click **+** (or equivalent add/import action).
4. Select the folder containing:
   - `ContainerGridAddin.py`
   - `ContainerGridAddin.manifest`
5. Run the add-in.
6. In the **Solid > Create** panel, click **Create Compartment Container**.
7. Set `LxWxD` (implemented as Length X, Height Y, Depth Z), wall/grid, and fillet values, then execute.

## Parameters and Interfaces

The add-in keeps a single source of truth through Fusion user parameters:

- `containerLength`
- `containerHeight`
- `containerDepth`
- `wallThickness`
- `rows`
- `cols`
- `bottomEdgeFilletWest`
- `bottomEdgeFilletEast`
- `bottomEdgeFilletSouth`
- `bottomEdgeFilletNorth`

Command inputs feed these parameters each run, so geometry generation and future updates rely on consistent named values.

## Internal Design Guide

- DRY: avoid duplicate geometry/math logic by centralizing calculations in helper functions.
- Single source of truth: all shared dimensions and behavior are parameter-backed.
- Open/closed: extend behavior through new helpers (new divider patterns, features) instead of rewriting core builders.
- Favor composition: build flow from focused functions (`ensure_parameters`, shell creation, divider creation, edge selection, filleting).
- Minimize side effects: isolate Fusion API mutations to build/apply functions; keep calculations pure where possible.

## Architecture Notes

Generation pipeline:

1. Validate command input.
2. Upsert user parameters.
3. Build outer solid and shell from top to create open-top container.
4. Build divider walls from interior bottom face.
5. Collect all bottom-edge segments for each compartment side and group by radius.
6. Apply grouped bottom-edge fillets only (N/S/E/W).

## Changelog

- **1.0.0**
  - Added Fusion 360 add-in command and manifest.
  - Implemented parameterized container dimensions and wall/grid controls.
  - Implemented compartment divider generation and orientation constraints.
  - Implemented side-specific bottom fillets (`North`, `South`, `East`, `West`) and shared all-compartment controls in the layout editor.
  - Updated edge collection so all matching compartment bottom-edge segments are targeted for fillets.
