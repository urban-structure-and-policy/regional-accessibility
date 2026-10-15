
import json
import os
import time
from contextlib import ExitStack
from datetime import datetime

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import requests
import shapely
from rasterio.crs import CRS
from rasterio.vrt import WarpedVRT
from shapely.geometry import LineString, box

HIGHWAY_CLASSES = [
    'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'unclassified',
    'residential', 'motorway_link', 'trunk_link', 'primary_link', 'secondary_link',
    'tertiary_link', 'living_street', 'service',
]
RETRY_STATUS = {429, 502, 503, 504}


# ---------------------------------------------------------------- helpers
def set_outpaths(worksp, region_name, date_tag, count_nearest_destinations, grid_space):
    """Output paths for one region (same naming as the QGIS version)."""
    output_folder = os.path.join(worksp, f'output/{region_name}_{count_nearest_destinations}_{date_tag}/')
    matrix_folder = os.path.join(output_folder, 'Matrizen')
    os.makedirs(matrix_folder, exist_ok=True)

    buffer_out = os.path.join(worksp, f'output/buffer/buffer_{region_name}.gpkg')
    points_out = os.path.join(worksp, f'output/osmpoints_{grid_space}mgrid_{region_name}.gpkg')
    points_ew_out = os.path.join(worksp, f'output/ew_points_{region_name}.gpkg')
    points_out_2 = os.path.join(worksp, f'output/osmpoints_mitEWsum_{grid_space}mgrid_{region_name}.gpkg')
    return output_folder, matrix_folder, buffer_out, points_out, points_ew_out, points_out_2


def _query_overpass(query, servers, timeout, user_agent, label, retries=6, backoff=3):
    """POST one query; rotate through `servers` on each attempt; back off between attempts.
    Returns parsed JSON or None if all attempts failed."""
    for attempt in range(1, retries + 1):
        server = servers[(attempt - 1) % len(servers)]
        try:
            # The public Overpass servers reject the default python-requests User-Agent (HTTP 406)
            resp = requests.post(server, data={'data': query}, timeout=timeout + 60,
                                 headers={'User-Agent': user_agent, 'Accept': '*/*'})
            if resp.status_code in RETRY_STATUS:
                raise RuntimeError(f'HTTP {resp.status_code}')
            resp.raise_for_status()  # other errors (e.g. 400 = bad query): not retried
            data = resp.json()
            if 'runtime error' in data.get('remark', ''):
                raise RuntimeError(data['remark'])
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                RuntimeError, ValueError) as e:
            if attempt == retries:
                print(f'Giving up on {label}: {e}')
                return None
            wait = backoff ** attempt
            print(f'Overpass problem on {server} ({e}), retrying in {wait}s...')
            time.sleep(wait)
        else:
            return data


def download_highways(region_name, bounds, worksp, crs, servers, timeout, user_agent,
                      tile_size=None, reuse=False):
    """Query the Overpass API for road ways within the bbox (bounds given in `crs`).
    With `tile_size` (meters) the bbox is split into tiles (smaller, more reliable queries).
    Returns parsed JSON (all tiles merged) or None if any tile failed."""
    raw_path = os.path.join(worksp, f'input/OSM/{region_name}_roh.json')
    if reuse and os.path.exists(raw_path):
        print(f'Using cached download {raw_path}')
        with open(raw_path, encoding='utf-8') as f:
            return json.load(f)

    xmin, ymin, xmax, ymax = bounds
    if tile_size is None:
        tiles = [bounds]
    else:
        tiles = [(x, y, min(x + tile_size, xmax), min(y + tile_size, ymax))
                 for x in np.arange(xmin, xmax, tile_size)
                 for y in np.arange(ymin, ymax, tile_size)]

    regex = '|'.join(HIGHWAY_CLASSES)
    elements = {}
    for i, tile in enumerate(tiles, start=1):
        west, south, east, north = gpd.GeoSeries([box(*tile)], crs=crs).to_crs('EPSG:4326').total_bounds
        query = (f'[out:json][timeout:{timeout}];'
                 f'way["highway"~"^({regex})$"]({south},{west},{north},{east});'
                 'out geom;')
        label = f'{region_name} (tile {i}/{len(tiles)})'
        if len(tiles) > 1:
            print(f'  {label}')
        data = _query_overpass(query, servers, timeout, user_agent, label)
        if data is None:
            return None
        for el in data.get('elements', []):
            elements[(el.get('type'), el.get('id'))] = el  # ways crossing tiles appear twice
        if i < len(tiles):
            time.sleep(1)  # be gentle with the public servers

    merged = {'elements': list(elements.values())}
    with open(raw_path, 'w', encoding='utf-8') as f:
        json.dump(merged, f)
    return merged


def overpass_to_lines(data, crs):
    """Overpass JSON ('out geom') -> GeoDataFrame of LineStrings in `crs`."""
    rows = []
    for el in data.get('elements', []):
        if el.get('type') != 'way' or 'geometry' not in el:
            continue
        coords = [(p['lon'], p['lat']) for p in el['geometry']]
        if len(coords) < 2:
            continue
        tags = el.get('tags', {})
        rows.append({'osm_id': el['id'], 'name': tags.get('name'),
                     'highway': tags.get('highway'), 'access': tags.get('access'),
                     'geometry': LineString(coords)})
    cols = ['osm_id', 'name', 'highway', 'access', 'geometry']
    if not rows:
        return gpd.GeoDataFrame(columns=cols, geometry='geometry', crs='EPSG:4326').to_crs(crs)
    return gpd.GeoDataFrame(rows, geometry='geometry', crs='EPSG:4326').to_crs(crs)


def filter_roads(highways):
    """Keep selected road types, drop access=private."""
    keep = highways['highway'].isin(HIGHWAY_CLASSES) & (highways['access'] != 'private')
    return highways[keep].reset_index(drop=True)


def make_hex_points(bounds, spacing, crs):
    """Centroids of a hexagon grid (QGIS 'Create grid', type Hexagon, spacing = flat-to-flat
    distance) = triangular point lattice with nearest-neighbour distance `spacing`."""
    xmin, ymin, xmax, ymax = bounds
    dx = spacing * np.sqrt(3) / 2
    nx = int((xmax - xmin) // dx) + 1
    ny = int((ymax - ymin) // spacing) + 1
    X, Y = np.meshgrid(xmin + dx * np.arange(nx), ymin + spacing * np.arange(ny))
    Y = Y + (np.arange(nx) % 2) * spacing / 2  # shift every second column by half a step
    keep = Y <= ymax
    x, y = X[keep], Y[keep]
    return gpd.GeoDataFrame({'id': np.arange(1, len(x) + 1)},
                            geometry=gpd.points_from_xy(x, y), crs=crs)


def snap_to_roads(points, highways, max_distance):
    """Move each point to the closest point on the nearest road (<= max_distance); drop others."""
    joined = gpd.sjoin_nearest(points, highways[['geometry']], how='inner', max_distance=max_distance)
    joined = joined[~joined.index.duplicated(keep='first')]  # exactly 1 neighbour per point
    lines = highways.geometry.loc[joined['index_right'].to_numpy()].to_numpy()
    pts = joined.geometry.to_numpy()
    snapped = shapely.line_interpolate_point(lines, shapely.line_locate_point(lines, pts))
    return gpd.GeoDataFrame({'id': joined['id'].to_numpy()}, geometry=snapped, crs=points.crs)


def raster_to_points(path, crs, ew_field, spacing=100, inset=50):
    """Regular point grid (spacing 100 m, inset 50 m) over the raster; sample raster values;
    keep only points with data (non-NoData)."""
    with ExitStack() as stack:
        src = stack.enter_context(rasterio.open(path))
        if src.crs != CRS.from_user_input(crs):
            src = stack.enter_context(WarpedVRT(src, crs=crs))
        left, bottom, right, top = src.bounds
        xs = np.arange(left + inset, right, spacing)
        ys = np.arange(bottom + inset, top, spacing)
        X, Y = np.meshgrid(xs, ys)
        coords = list(zip(X.ravel(), Y.ravel()))
        samples = list(src.sample(coords, indexes=1, masked=True))
    vals = np.ma.concatenate(samples).astype('float64').filled(np.nan)
    gdf = gpd.GeoDataFrame({ew_field: vals},
                           geometry=gpd.points_from_xy(X.ravel(), Y.ravel()), crs=crs)
    return gdf[gdf[ew_field].notna()].reset_index(drop=True)


def sum_population_per_point(points, ew_points, ew_field, extent_buffer_pct=2.0):
    """Population sum per road point = sum over its Voronoi cell.
    A population point lies in the Voronoi cell of its nearest road point, so a nearest-neighbour
    join is equivalent. Cells are bounded by the point extent grown by `extent_buffer_pct` %
    (as in QGIS), so population points outside that extent are ignored."""
    minx, miny, maxx, maxy = points.total_bounds
    ex = (maxx - minx) * extent_buffer_pct / 100
    ey = (maxy - miny) * extent_buffer_pct / 100
    extent = box(minx - ex, miny - ey, maxx + ex, maxy + ey)

    ew = ew_points.loc[ew_points.intersects(extent), [ew_field, 'geometry']]
    nearest = gpd.sjoin_nearest(ew, points[['id', 'geometry']], how='inner')
    nearest = nearest[~nearest.index.duplicated(keep='first')]
    return nearest.groupby('id')[ew_field].sum()


# ---------------------------------------------------------------- main
def run(worksp,
        umkreis=10000,               # buffer in meters, same value as in the next scripts
        crs='EPSG:25832',            # all data are transformed to this CRS
        pct=50,
        grid_space=1000,
        ew_field='Einwohner',
        region_field='region',
        zensus_geomtype='Point',     # 'Point', 'Polygon' or 'Raster'
        overpass_servers=('https://overpass-api.de/api/interpreter',
                          'https://lz4.overpass-api.de/api/interpreter'),  # tried in turn on retries
        tile_size=None,              # e.g. 20000 (m) to split the region bbox into tiles
        overpass_timeout=600,
        user_agent='accessibility-analysis/1.0 (contact: your.name@example.org)',  # use a real contact
        reuse_download=False,
        date_tag=None,
        municipality_file = "municipalities.gpkg"):              # 'yy_mm_dd' for the dated output folders; None = today
    # values passed from R arrive as floats -> make sure integers are integers
    pct, grid_space, overpass_timeout = int(pct), int(grid_space), int(overpass_timeout)
    if isinstance(overpass_servers, str):
        overpass_servers = [overpass_servers]
    if tile_size is not None:
        tile_size = float(tile_size)
    now = datetime.now()
    date_tag = date_tag or now.strftime('%y_%m_%d')
    print('Start:', now.strftime('%H:%M:%S'))
    count_nearest_destinations = f'{pct}perc'

    if zensus_geomtype not in ('Point', 'Polygon', 'Raster'):
        raise ValueError("Zensus has incompatible data type, please provide 'Point', 'Polygon' or 'Raster'.")

    # --- folder setup
    for folder in ['input/OSM', 'output', 'output/buffer']:
        os.makedirs(os.path.join(worksp, folder), exist_ok=True)

    # --- file inputs
    municip_path = os.path.join(worksp, 'input', municipality_file)   # all municipalities, field `region`
    zensus = os.path.join(worksp, 'input', 'zensus2022_ew_buffer.gpkg')  # output from preprocessing step
    for p in (municip_path, zensus):
        if not os.path.exists(p):
            raise FileNotFoundError(f'Missing file: {p}')

    # --- read and prepare data
    municip = gpd.read_file(municip_path).to_crs(crs)
    regions = municip.dissolve(by=region_field)   # complete regions to iterate over
    # or if already dissolved: regions = municip.set_index(region_field)
    buffer = regions.copy()
    buffer.geometry = regions.buffer(umkreis)

    if zensus_geomtype == 'Raster':
        print('> Zensus as raster layer, extracting point grid with spacing 100m...')
        zensus_all = raster_to_points(zensus, crs, ew_field)
    else:
        zensus_all = gpd.read_file(zensus).to_crs(crs)

    for region_name, geom in regions.geometry.items():
        print('\n--- Region:', region_name, '---')
        (output_folder, matrix_folder, buffer_out,
         points_out, points_ew_out, points_out_2) = set_outpaths(
            worksp, region_name, date_tag, count_nearest_destinations, grid_space)

        buffer_geom = buffer.geometry.loc[region_name]
        buffer.loc[[region_name]].to_file(buffer_out, driver='GPKG')
        bounds = geom.bounds  # (xmin, ymin, xmax, ymax) of the region

        # --- OSM roads
        print(datetime.now(), 'Download OSM Data...')
        data = download_highways(region_name, bounds, worksp, crs,
                                 list(overpass_servers), overpass_timeout, user_agent,
                                 tile_size=tile_size, reuse=reuse_download)
        if data is None:
            print(f'Skipping {region_name}: could not download.')
            continue
        print(f'Downloaded region {region_name}.')

        print(datetime.now(), 'Filter roads...')
        highways = filter_roads(overpass_to_lines(data, crs))
        if highways.empty:
            print(f'Skipping {region_name}: no roads found.')
            continue

        # --- origin points: grid -> snap to roads -> clip to buffer -> deduplicate
        print(datetime.now(), 'Raster points for road network...')
        grid_points = make_hex_points(bounds, grid_space, crs)
        snapped = snap_to_roads(grid_points, highways, max_distance=grid_space / 2)
        snapped = snapped[snapped.intersects(buffer_geom)]
        snapped = snapped[~snapped.geometry.to_wkb().duplicated()].reset_index(drop=True)
        snapped.to_file(points_out, driver='GPKG')

        # --- population points
        print(datetime.now(), 'Add population data to points...')
        if zensus_geomtype == 'Polygon':
            print('> Zensus as polygons, getting centroids...')
            clipped = gpd.clip(zensus_all, buffer_geom, keep_geom_type=True)
            ew_points = clipped.copy()
            ew_points['geometry'] = clipped.geometry.centroid
        elif zensus_geomtype == 'Raster':
            ew_points = zensus_all
        else:
            print('> Zensus already as points.')
            ew_points = zensus_all
        ew_points.to_file(points_ew_out, driver='GPKG')
        if ew_field not in ew_points.columns:
            raise KeyError(f"Field '{ew_field}' not found in population data: {list(ew_points.columns)}")

        # --- population sum per road point (Voronoi logic)
        print(datetime.now(), 'Einwohnerdaten auf Straßenpunkte summieren...')
        sums = sum_population_per_point(snapped, ew_points, ew_field)
        result = snapped.copy()
        result[f'{ew_field}_sum'] = result['id'].map(sums)
        s = result[f'{ew_field}_sum']
        result['EW_2'] = s.where(s >= 1, 0.0)  # points with < 1 inhabitant (or none) -> 0
        result.to_file(points_out_2, driver='GPKG')

    end = datetime.now()
    print('Ende:', end.strftime('%H:%M:%S'))


if __name__ == '__main__':
    run(worksp='path/to/workspace/')
