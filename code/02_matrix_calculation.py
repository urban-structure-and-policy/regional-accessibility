"""
Script 02 (standalone, no QGIS): random subset of road points, population sums,
destination selection and ORS distance/duration matrices.

Needs the output of skript_01_vorbereitung.py (must be importable, i.e. in the same folder).
Required packages: geopandas, shapely>=2, pyogrio, numpy, pandas, openrouteservice (pip)
"""
import logging
import os
import sys
from datetime import datetime

import geopandas as gpd
import numpy as np
import openrouteservice
import pandas as pd

from a01 import sum_population_per_point

# Column names of the matrix CSVs.
COL_FROM, COL_TO, COL_DIST, COL_DUR = 'FROM_ID', 'TO_ID', 'DIST_KM', 'DURATION_H'


# ---------------------------------------------------------------- helpers
def set_outpaths(worksp, region_name, date_tag, count_nearest_destinations, grid_space):
    output_folder = os.path.join(worksp, f'output/{region_name}_{count_nearest_destinations}_{date_tag}/')
    matrix_folder = os.path.join(output_folder, 'Matrizen')
    os.makedirs(matrix_folder, exist_ok=True)

    point_path = os.path.join(worksp, f'output/osmpoints_mitEWsum_{grid_space}mgrid_{region_name}.gpkg')
    points_ew_out_s1 = os.path.join(worksp, f'output/ew_points_{region_name}.gpkg')
    extract_file = os.path.join(output_folder, f'random_extract_{region_name}.gpkg')
    destins_10perc_out = os.path.join(output_folder, f'{region_name}_10perc_ew.gpkg')
    destins_out = os.path.join(
        output_folder, f'destination_points_{grid_space}mgrid_{region_name}{count_nearest_destinations}.gpkg')
    return (output_folder, matrix_folder, point_path, points_ew_out_s1,
            extract_file, destins_10perc_out, destins_out)


def query_matrix(client, origin_id, origin_xy, ids, xy, profile):
    """One ORS matrix request: 1 origin -> many destinations. Coordinates are (lon, lat)."""
    locations = [list(origin_xy)] + xy.tolist()
    res = client.distance_matrix(
        locations=locations,
        profile=profile,
        sources=[0],
        destinations=list(range(1, len(locations))),
        metrics=['distance', 'duration'],
        units='km',
        validate=False,
    )
    dist = np.array(res['distances'][0], dtype=float)           # km
    dur = np.array(res['durations'][0], dtype=float) / 3600.0   # seconds -> hours
    return pd.DataFrame({COL_FROM: origin_id, COL_TO: ids, COL_DIST: dist, COL_DUR: dur})


def matrix_with_split(client, origin_id, origin_xy, ids, xy, profile, depth):
    """Try all destinations at once; on failure split the destinations in two halves
    (`depth` = how many times splitting is allowed; 1 = behaviour of the QGIS version)."""
    try:
        return query_matrix(client, origin_id, origin_xy, ids, xy, profile)
    except Exception as e:
        if depth <= 0 or len(ids) < 2:
            raise
        logging.info(str(e))
        logging.info('ORS failed, attempting split-mode...')
        mid = len(ids) // 2
        parts = [
            matrix_with_split(client, origin_id, origin_xy, ids[:mid], xy[:mid], profile, depth - 1),
            matrix_with_split(client, origin_id, origin_xy, ids[mid:], xy[mid:], profile, depth - 1),
        ]
        return pd.concat(parts, ignore_index=True)


# ---------------------------------------------------------------- main
def run(worksp,
        pct=50,
        grid_space=1000,
        ew_field='Einwohner',
        region_field='region',
        get_matrix=True,
        reuse_selection=True,          # reuse previous random selection (file in the dated output folder)
        date_tag=None,                 # 'yy_mm_dd' of the output folder to use; None = today
        random_seed=None,              # set an integer for a reproducible random selection
        ors_url="http://url/ors",
        ors_profile='driving-car',
        ors_timeout=300,
        max_split_depth=1,
        municipality_file = "municipalities.gpkg"):
    now = datetime.now()
    print('Start: ' + now.strftime('%y/%m/%d/%H:%M:%S'))
    count_nearest_destinations = f'{pct}perc'
    date_tag = date_tag or now.strftime('%y_%m_%d')

    municip_path = os.path.join(worksp, 'input', municipality_file)

    # --- logging (force=True replaces existing handlers, e.g. in RStudio/Jupyter)
    log_file = os.path.join(worksp, f"run_log_{now.strftime('%y_%m_%d_%H%M%S')}.txt")
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[logging.FileHandler(log_file, mode='w', encoding='utf-8'),
                  logging.StreamHandler(sys.stdout)],
        force=True,
    )
    logging.info('Start: ' + now.strftime('%y/%m/%d/%H:%M:%S'))

    # regions are only needed by name (same order as the dissolved layer in the QGIS version)
    region_names = sorted(gpd.read_file(municip_path)[region_field].unique())

    client = openrouteservice.Client(base_url=ors_url, timeout=ors_timeout) if get_matrix else None

    for region_name in region_names:
        logging.info(f'\n--- Region: {region_name} ---')
        (output_folder, matrix_folder, point_path, points_ew_out_s1,
         extract_file, destins_10perc_out, destins_out) = set_outpaths(
            worksp, region_name, date_tag, count_nearest_destinations, grid_space)

        logging.info('Load population points...')
        ew_points = gpd.read_file(points_ew_out_s1)

        # --- random subset of road points (reduces computation time)
        logging.info(f'Keeping {pct}% of road network points...')
        if reuse_selection and os.path.exists(extract_file):
            logging.info(f'> Reusing previous random selection for {region_name}...')
            extract = gpd.read_file(extract_file)
        else:
            logging.info(f'> Generating new random selection for {region_name}...')
            extract = gpd.read_file(point_path).sample(frac=pct / 100, random_state=random_seed).sort_index()
            extract = extract[~extract.geometry.to_wkb().duplicated()].reset_index(drop=True)
            extract.to_file(extract_file, driver='GPKG')

        # --- population per point for the reduced point set (like script 01, with the subset)
        logging.info('Add population data to points...')
        sums = sum_population_per_point(extract, ew_points, ew_field)
        sum_col = f'{ew_field}_sum_2'   # suffix _2: `{ew_field}_sum` already exists from script 01
        region_points = extract.copy()
        region_points[sum_col] = region_points['id'].map(sums)
        s = region_points[sum_col]
        region_points['EW_10'] = s.where(s >= 1, 0.0)  # < 1 inhabitant (or none) -> 0
        region_points.to_file(destins_10perc_out, driver='GPKG')

        logging.info('select destination points')
        destinations = region_points[region_points['EW_10'] >= 5].reset_index(drop=True)
        destinations.to_file(destins_out, driver='GPKG')

        if not get_matrix:
            logging.info('Keine Matrizenberechnung.')
            continue
        if destinations.empty:
            logging.info('No destination points (EW_10 >= 5), skipping matrices.')
            continue

        # --- matrices: one request per origin point (ORS expects lon/lat)
        logging.info('Matrix calculation')
        err = []
        origins_ll = region_points.to_crs('EPSG:4326')
        dests_ll = destinations.to_crs('EPSG:4326')
        dest_ids = dests_ll['id'].astype(int).to_numpy()
        dest_xy = np.column_stack([dests_ll.geometry.x.to_numpy(), dests_ll.geometry.y.to_numpy()])

        for point_id, geom in zip(origins_ll['id'].astype(int), origins_ll.geometry):
            logging.info(point_id)
            matrix_out = os.path.join(matrix_folder, f'matrix_{region_name}_{grid_space}mgrid_{point_id}.csv')

            # skip existing files if they are not empty (minimum size)
            if os.path.isfile(matrix_out) and os.stat(matrix_out).st_size > 100:
                logging.info(f'Matrix for point {point_id} already exists. Skipping...')
                continue

            # Common error: ORS code 6020 "Search exceeds the limit of visited nodes" (dense city centers),
            # also code 6010 for points that cannot be snapped to a drivable road (see error list below).
            try:
                df = matrix_with_split(client, point_id, (geom.x, geom.y), dest_ids, dest_xy,
                                       ors_profile, max_split_depth)
                df.to_csv(matrix_out, index=False)
            except Exception as e:
                logging.info(str(e))
                logging.info(f'Split mode failed. Adding {point_id} to error list')
                err.append(point_id)

        logging.info('Building err_points layer...')
        if err:
            logging.info(f'> {len(err)} points failed. Creating error geometry layer.')
            err_points = region_points[region_points['id'].astype(int).isin(err)]
            err_points.to_file(
                os.path.join(output_folder, 'err_points_' + now.strftime('%y_%m_%d') + '.gpkg'),
                driver='GPKG')
            logging.info('> Saved error points to region folder')
        else:
            logging.info('> No ORS errors detected. No err_points layer created.')

    end = datetime.now()
    logging.info('End of script.')
    logging.info(f'Runtime: {end - now}')


if __name__ == '__main__':
    run(worksp='path/to/worksp')
