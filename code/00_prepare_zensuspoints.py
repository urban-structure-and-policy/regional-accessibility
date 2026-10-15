import geopandas as gpd
import pandas as pd
from shapely.geometry import Point
import os

def run(worksp,
        umkreis=10000,               # buffer in meters, same value as in the next scripts
        crs='EPSG:25832',            # all data are transformed to this CRS
        zensus_crs = 3035,
        municipality_file='municipalities.gpkg',
        zensus_file='Zensus2022_Bevoelkerungszahl_100m-Gitter.csv'
        ):
          
    # file inputs
    municip_path = os.path.join(worksp, 'input', municipality_file)  # here: all municipalities in regions of interest with field `region` indicating municipalities belonging together
    zensus_csv_path = os.path.join(worksp, 'input', zensus_file)  # here: csv with field for x and y coordinates of centroids of 100m grid
    zensus_out = os.path.join(worksp, 'input', 'population_buffer.gpkg')  # output after pre-processing

    ## buffer
    
    ## get buffer to only extract zensus points for regions of interest
    municip = gpd.read_file(municip_path).to_crs(crs)
    regions = municip.dissolve(by='region')
    buffer = regions.copy()
    buffer.geometry = regions.buffer(umkreis)

    # zensus points
    
    ## Population data is available in csv with x and y coordinates of centroid of 100m grid -> transform this to point geometries
    zensus_csv = pd.read_csv(zensus_csv_path, sep=";")
    zensus_csv["geometry"] = [Point(xy) for xy in zip(zensus_csv["x_mp_100m"], zensus_csv["y_mp_100m"])]
    zensus_gdf = gpd.GeoDataFrame(zensus_csv, geometry = "geometry")
    # set crs the original coordinates were in and transform to working crs
    zensus_gdf.set_crs(epsg=zensus_crs, inplace=True).to_crs(crs, inplace = True)  
    
    # clip zensus points to buffer
    zensus_clip = zensus_gdf.clip(buffer)
    
    # export
    zensus_clip.to_file(zensus_out, driver = "GPKG")

if __name__ == '__main__':
    run(worksp='path/to/worksp')
