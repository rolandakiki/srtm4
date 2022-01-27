#!/usr/bin/env python
# vim: set fileencoding=utf-8
# pylint: disable=C0103

"""
Module to download terrain digital elevation models from the SRTM 90m DEM.

Copyright (C) 2016, Carlo de Franchis <carlo.de-franchis@ens-cachan.fr>

For more info on the srtm90 dataset, check https://srtm.csi.cgiar.org/faq/
The AREA_OR_POINT tag is wrong in the geotiff meta. 
(It is set to Area when it should be Point).
In this code, we do not touch the tag, but only correct the effect by taking into
account that the transform's origin is the center of the first pixel.

"""

from __future__ import print_function
import subprocess
import zipfile
import sys
import os

import numpy as np
import requests
from requests.adapters import HTTPAdapter, Retry, RetryError
import filelock

import affine
import pyproj

# network access to datum grids
pyproj.network.set_network_enabled(active=True)

import rasterio

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin')
GEOID = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

SRTM_DIR = os.getenv('SRTM4_CACHE')
if not SRTM_DIR:
    SRTM_DIR = os.path.join(os.path.expanduser('~'), '.srtm')

SRTM_URL = 'https://srtm.csi.cgiar.org/wp-content/uploads/files/srtm_5x5/TIFF'

TILE_SIZE = 6000
# degree resolution
RES = 3 / 3600

def _requests_retry_session(
        retries=5,
        backoff_factor=0.3,
        status_forcelist=(500, 502, 503, 504),
):
    """
    Makes a requests object with built-in retry handling with
    exponential back-off on 5xx error codes.
    """
    session = requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def download(to_file, from_url):
    """
    Download a file from the internet.

    Args:
        to_file: path where to store the downloaded file
        from_url: url of the file to download

    Raises:
        RetryError: if the `get` call exceeds the number of retries
            on 5xx codes
        ConnectionError: if the `get` call does not return a 200 code
    """
    # Use a requests session with retry logic because the server at
    # SRTM_URL sometimes returns 503 responses when overloaded
    session = _requests_retry_session()
    r = session.get(from_url, stream=True)
    if not r.ok:
        raise ConnectionError(
            "Response code {} received for url {}".format(r.status_code, from_url)
        )
    file_size = int(r.headers['content-length'])
    print("Downloading: {} Bytes: {}".format(to_file, file_size),
          file=sys.stderr)

    with open(to_file, 'wb') as f:
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:  # filter out keep-alive new chunks
                f.write(chunk)


def get_srtm_tile(srtm_tile, out_dir):
    """
    Download and unzip an srtm tile from the internet.

    Args:
        srtm_tile: string following the pattern 'srtm_%02d_%02d', identifying
            the desired strm tile
        out_dir: directory where to store and extract the srtm tiles
    """
    output_dir = os.path.abspath(os.path.expanduser(out_dir))
    try:
        os.makedirs(output_dir)
    except OSError:
        pass

    srtm_zip_download_lock = os.path.join(output_dir, 'srtm_zip.lock')
    srtm_tif_write_lock = os.path.join(output_dir, 'srtm_tif.lock')

    if os.path.exists(os.path.join(output_dir, '{}.tif'.format(srtm_tile))):
        # the tif file is either being written or finished writing
        # locking will ensure it is not being written.
        # Also by construction we won't write on something complete.
        lock_tif = filelock.FileLock(srtm_tif_write_lock)
        lock_tif.acquire()
        lock_tif.release()
        return

    # download the zip file
    srtm_tile_url = '{}/{}.zip'.format(SRTM_URL, srtm_tile)
    zip_path = os.path.join(output_dir, '{}.zip'.format(srtm_tile))

    lock_zip = filelock.FileLock(srtm_zip_download_lock)
    lock_zip.acquire()

    if os.path.exists(os.path.join(output_dir, '{}.tif'.format(srtm_tile))):
        # since the zip lock is returned after the tif lock
        # if we end up here, it means another process downloaded the zip
        # and extracted it.
        # No need to wait on lock_tif
        lock_zip.release()
        return

    if os.path.exists(zip_path):
        print('zip already exists')
        # Only possibility here is that the previous process was cut short

    try:
        download(zip_path, srtm_tile_url)
    except (ConnectionError, RetryError) as e:
        lock_zip.release()
        raise e

    lock_tif = filelock.FileLock(srtm_tif_write_lock)
    lock_tif.acquire()

    # extract the tif file
    if zipfile.is_zipfile(zip_path):
        z = zipfile.ZipFile(zip_path, 'r')
        z.extract('{}.tif'.format(srtm_tile), output_dir)
    else:
        print('{} not available'.format(srtm_tile))

    # remove the zip file
    os.remove(zip_path)

    # release locks
    lock_tif.release()
    lock_zip.release()


def lon_lats_str(lon, lat):
    """
    Make a lon_lats string that can be passed to the
    srtm4 binaries

    Args:
        lon, lat: lists of longitudes and latitudes (same length), or single
            longitude and latitude

    Returns:
        str: lon_lats string
    """
    try:
        lon_lats = '\n'.join('{} {}'.format(a, b) for a, b in zip(lon, lat))
    except TypeError:
        lon_lats = '{} {}'.format(lon, lat)
    return lon_lats


def srtm4_which_tile(lon, lat):
    """
    Determine the srtm tiles needed to cover the (list of) point(s)
    by running the srtm4_which_tile binary

    Args:
        lon, lat: lists of longitudes and latitudes (same length), or single
            longitude and latitude

    Returns:
        list of str: list of srtm tile names
    """
    # run the srtm4_which_tile binary and feed it from stdin
    lon_lats = lon_lats_str(lon, lat)
    p = subprocess.Popen(['srtm4_which_tile'], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE,
                         env={'PATH': BIN, 'SRTM4_CACHE': SRTM_DIR})
    outs, errs = p.communicate(input=lon_lats.encode())

    # read the list of needed tiles
    srtm_tiles = outs.decode().split()
    return srtm_tiles


def srtm4(lon, lat):
    """
    Gives the SRTM height of a (list of) point(s).

    Args:
        lon, lat: lists of longitudes and latitudes (same length), or single
            longitude and latitude

    Returns:
        height(s) in meters above the WGS84 ellipsoid (not the EGM96 geoid)
    """
    # get the names of srtm_tiles needed
    srtm_tiles = srtm4_which_tile(lon, lat)

    # download the tiles if not already there
    for srtm_tile in set(srtm_tiles):
        get_srtm_tile(srtm_tile, SRTM_DIR)

    # run the srtm4 binary and feed it from stdin
    lon_lats = lon_lats_str(lon, lat)
    p = subprocess.Popen(['srtm4'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         env={'PATH': BIN,
                              'SRTM4_CACHE': SRTM_DIR,
                              'GEOID_PATH': GEOID})
    outs, errs = p.communicate(input=lon_lats.encode())

    # return the altitudes
    alts = list(map(float, outs.decode().split()))
    return alts if isinstance(lon, (list, np.ndarray)) else alts[0]

def name2id(tile_name):
    """
    Convert the tile name to the lon, lat ids.

    Args:
        tile_name: "srtm_lonid_latid" (str)
    Returns:
        srtm tile: lon_id (int) and lat_id (int)
            lon_id: 1 to 72 from -180 to 180 with a step of 5 degrees
            lat: 1 to 24  from 60 to -60 with a step of 5 degrees 
    """
    splitted = tile_name.split('_')
    lon_id = int(splitted[1])
    lat_id = int(splitted[2])
    return lon_id, lat_id


def id2name(lon_id, lat_id):
    """
    Convert the lon, lat ids to the corresponding tile name. 

    Args: 
        lon_id (int) and lat_id (int)
    Returns:
        tile_name "srtm_lonid_latid" (str)
    """
    return 'srtm_{:02d}_{:02d}'.format(lon_id, lat_id)


def assert_interval(interval):
    """Assert that the passed tuple is an interval.

    Args: 
        interval: tuple (low, up)
    """
    low, up = interval
    assert up > low, "Must be an interval"


def intersect_intervals(interval_one, interval_two):
    """
    Find the intersection of two intervals. 

    Args: 
        interval_one: tuple (low_one, up_one)
        interval_two: tuple (low_two, up_two)
    Returns: 
        intersection tuple (low_inter, up_inter).
        If no intersection is found, return (None, None)
    """
    assert_interval(interval_one)
    assert_interval(interval_two)

    low_one, up_one = interval_one
    low_two, up_two = interval_two

    low_inter = max(low_one, low_two)
    up_inter = min(up_one, up_two)

    if low_inter > up_inter:
        return (None, None)

    else:
        return (low_inter, up_inter)


def adjust_bounds_to_px_grid(bounds):
    """
    Adjust the bounds to fall exactly onto the pixel grid (center of pixels) of SRTM90.

    Args: 
        bounds: tuple (lon_min, lat_min, lon_max, lat_max) 

    Returns: 
        adjusted_bounds: tuple (lon_min, lat_min, lon_max, lat_max) 
                        The bounds are of adjusted on the center of the pixels. 
                        The last pixel (max) is included in the image.
        transform: affine.Affine px_is_area transform
        shape: tuple (height, width) shape of the output image.         

    """
    lon_min, lat_min, lon_max, lat_max = bounds

    # Adjust to pixel grid
    col_min = int(np.floor(lon_min / RES))
    col_max = int(np.ceil(lon_max / RES))
    row_min = int(np.floor(lat_min / RES))
    row_max = int(np.ceil(lat_max / RES))

    lon_min = RES * col_min
    lon_max = RES * col_max
    lat_min = RES * row_min
    lat_max = RES * row_max
    
    adjusted_bounds = (lon_min, lat_min, lon_max, lat_max)
    # Translate by half a pixel for the px_is_area transform( upper left corner)
    transform = affine.Affine(RES, 0, lon_min - RES/2, 0, -RES, lat_max + RES/2)
    shape =  (row_max - row_min + 1, col_max - col_min + 1)
    
    return adjusted_bounds, transform, shape


def to_ellipsoid(lons, lats, alts):
    """
    Convert geoidal heights to ellipsoidal heights.

    Args:
        lats, lons (array): 1D arrays of latitudes and longitudes
        alts (array): 1D array of altitudes, referenced to the geoid

    Returns:
        (array): altitudes referenced to the ellipsoid
    """
    dtype = alts.dtype
    # WGS84 with ellipsoid height as vertical axis
    ellipsoid = pyproj.CRS.from_epsg(4979)

    # WGS84 with Gravity-related height (EGM96)
    geoid = pyproj.CRS("EPSG:4326+5773")

    trf = pyproj.Transformer.from_crs(geoid, ellipsoid)

    # check that pyproj actually modified the values
    new_alt = trf.transform(lats, lons, alts)[-1]

    assert np.any(new_alt != alts)
    return np.around(new_alt, 5).astype(dtype)


def intersect_bounds(one_bound, other_bound):
    """Intersect two bounds. 

    Args:
        one_bound, other_bound: tuple of float

    Returns:
        tuple of float. 
        If no intersection is found, tuple of None returned. 
    """
    one_w, one_s, one_e, one_n = one_bound
    other_w, other_s, other_e, other_n = other_bound

    int_w, int_e = intersect_intervals((one_w, one_e), (other_w, other_e))
    int_s, int_n = intersect_intervals((one_s, one_n), (other_s, other_n))

    intersection_bounds = int_w, int_s, int_e, int_n

    if np.any([a is None for a in intersection_bounds]):
        return (None, None, None, None)
    else:
        return intersection_bounds


def special_round(val, eps=1e-2):
    """
    Round values to nearest integer while making sure that the rounding error\
        is below eps. 

    Args:
        val: float value to be rounded. 
        eps: float, optional.
            The rounding is considered as valid if the error is less than eps.
            The default is 1e-2.
    Returns: 
        rounded: int, rounded value. 
    """
    rounded = round(val)
    assert rounded - val < eps, "Big rounding error induced !"
    return rounded


def get_px_region(geo_px_bounds, transform, transform_is_area=True):
    """
    Get the pixel region of some geographic bounds using the transform.

    Args:
        geo_px_bounds: tuple (lon_min, lat_min, lon_max, lat_max)
                       Geographic coords bounds previously fitted on the 
                       center of the pixels.
        transform: affine.Affine transform.
        transform_is_area: boolean indicating if the transform's origin
                           in the pixel coordinates is the upper left corner of the 
                           first pixel(True) or the center of the first_pixel(False), optional.
                           The default is True.
    Returns: 
        tuple (col, row, w, h) of the px region in the image.
    """
    window = rasterio.windows.from_bounds(*geo_px_bounds, transform)

    if transform_is_area:
        off = 0.5
    else:
        off = 0

    row, col = special_round(
        window.row_off - off), special_round(window.col_off - off)
    h, w = special_round(window.height), special_round(window.width)

    return (col, row, w, h)


def merge(datasets, transform, shape, nodata=np.nan, dtype="f4"):
    """
    Merge multiple rasterio datasets into a final array.

    Args:
        datasets: list of opened rasterio datasets.
        transform: affine.Affine transform of the final image in px_is_area convention.
        shape: tuple (height, width) shape of the final image. 
        nodata: nodata value in the final array, optional.
                The default is np.nan.
        dtype: Type of data in the final array. the default is "f4".

    Returns
        dst_array: 2D array (image) containing the merged datasets. 
    """
    dst_array = np.full(shape, nodata, dtype=dtype)
    dst_height, dst_width = shape

    # get bounds on pixels, where last pixel line/column not included in the image
    off = 0.5
    dst_w, dst_n = transform * (off, off)
    dst_e, dst_s = transform * (dst_width + off, dst_height + off)

    dst_bounds = (dst_w, dst_s, dst_e, dst_n)

    def copyto(old_data, new_data, old_nodata, new_nodata):
        mask = np.logical_and(old_nodata, ~new_nodata)
        old_data[mask] = new_data[mask]

    for dataset in datasets:
        # compute intersection
        int_bounds = intersect_bounds(dst_bounds, dataset.bounds)

        w, s, e, n = int_bounds
        if w is None:  # empty intersection, skip
            continue

        # compute dest window in dst_array

        col_dst, row_dst, width_dst, height_dst = get_px_region(
            int_bounds, transform)
        col, row, width, height = get_px_region(int_bounds, dataset.transform,
                                                transform_is_area=False)

        # read source
        tmp_array = dataset.read(1,
                                 window=((row, row + height),
                                         (col, col + width))
                                 )

        # write tmp_array into dst_region
        dst_region = dst_array[row_dst: row_dst +
                               height_dst, col_dst: col_dst + width_dst]
        mask_region = np.isnan(dst_region) if np.isnan(
            nodata) else dst_region == nodata
        mask_tmp = np.isnan(tmp_array) if np.isnan(
            dataset.nodata) else tmp_array == dataset.nodata

        copyto(dst_region, tmp_array.astype(dtype), mask_region, mask_tmp)

    return dst_array


def crop_at_continous_lon_limits(bounds, datum="ellipsoidal"):
    """
    Computes a crop of SRTM90 from the specified bounds.
    It is assumed that the bounds do not cross the antimeridian.

    Args: 
        bounds: geospatial bound tuple (lon_min, lat_min, lon_max, lat_max).
        datum: str, either "ellipsoidal" or "orthometric". SRTM90 tiles are 
            by default orthometric (w.r.t. the egm96_15 geoid). If "orthometric"
            is selected, the tiles are simply stitched together. When "ellipsoidal"
            is selected, a datum shift will also be applied and the height will be 
            referenced to the ellipsoid.
    Returns:
        raster: np.2darray of the dem crop
        transform: affine.Affine transform in px_is_area convention
        crs: rasterio.crs.CRS. always epsg:4326, even if orthometric (2D crs)
        
    """
    assert datum in [
        "ellipsoidal", "orthometric"], "Datum must be either ellipsoidal or orthometric"

    lon_min, lat_min, lon_max, lat_max = bounds

    lat_min, lat_max = intersect_intervals((lat_min, lat_max), (-60, 60))

    if lat_min is None:
        print("Lat coordinates out of coverage, crop will be skipped")
        return None, None, None

    bounds, transform, dem_shape = adjust_bounds_to_px_grid(
        (lon_min, lat_min, lon_max, lat_max))

    lon_min, lat_min, lon_max, lat_max = bounds

    # get lon lat array for bounding pts
    lons = [lon_min, lon_max]
    lats = [lat_max, lat_min]

    # get tile ids for bounding pts
    tiles = srtm4_which_tile(lons, lats)
    parsed_tiles = [name2id(t) for t in tiles]

    # all intermediate tile ids
    lon_ids = [t[0] for t in parsed_tiles]
    lat_ids = [t[1] for t in parsed_tiles]

    # the way the tiles are id'ed, they should be ordered already

    assert lon_ids[0] <= lon_ids[1]
    lon_id = np.arange(lon_ids[0], lon_ids[1] + 1)

    assert lat_ids[0] <= lat_ids[1]
    lat_id = np.arange(lat_ids[0], lat_ids[1] + 1)

    datasets = []
    for lat in lat_id:
        for lon in lon_id:
            try:
                # download tile if not already on the disk
                get_srtm_tile(id2name(lon, lat), SRTM_DIR)
            except ConnectionError:
                continue
            else:
                # open, read relevant rows and cols
                tile_name = os.path.join(SRTM_DIR, id2name(lon, lat) + '.tif')
                db = rasterio.open(tile_name, 'r')
                datasets.append(db)
                
    if len(datasets) == 0:
        raise ValueError("No DEM found on bounds")

    raster = merge(datasets, transform=transform, shape=dem_shape)

    if datum == "ellipsoidal":
        shape = raster.shape

        # get dem points in crs
        col, row = np.meshgrid(
            np.arange(shape[1]), np.arange(shape[0]))

        col = col.ravel() + 0.5
        row = row.ravel() + 0.5

        # to earth coordinates
        lon, lat = transform * (col, row)
        # reshape
        lon = lon.reshape(shape)
        lat = lat.reshape(shape)
        
        raster = to_ellipsoid(lon, lat, raster)
    
    crs = rasterio.crs.CRS.from_epsg(4326)
    return raster, transform, crs
     


def wrap_lon(lon):
    """Wrap the longitude to the [-180, 180[ interval."""
    return (lon + 180) % 360 - 180


def crop(bounds, datum="orthometric"):
    """
    Get a crop of the SRTM90 dem at the specified bounds.\
        The bounds can intersect the antimeridian.

    Args: 
        bounds: geospatial bound tuple (lon_min, lat_min, lon_max, lat_max).
                Examples of bounds that intersects the antimeridian: 
                    (-185, lat_min, -175, lat_max) or (175, lat_min, 185, lat_max).
        datum: str, either "ellipsoidal" or "orthometric". SRTM90 tiles are 
            by default orthometric (w.r.t. the egm96_15 geoid). If "orthometric"
            is selected, the tiles are simply stitched together. When "ellipsoidal"
            is selected, a datum shift will also be applied and the height will be 
            referenced to the ellipsoid. The default is "orthometric".

    Returns:
        raster: np.2darray of the dem crop
        transform: affine.Affine transform in px_is_area convention
        crs: rasterio.crs.CRS. always epsg:4326, even if orthometric (2D crs)
    """
    lon_start, lat_min, lon_end, lat_max = bounds
    offset = 0.1 * RES
    assert lon_start <= lon_end, "Not valid lon interval"
    lon_start = wrap_lon(lon_start)
    lon_end = wrap_lon(lon_end)
    # in this case , wrapping has occured on the interval at antimeridian
    if lon_end < lon_start:

        bounds_start = lon_start, lat_min, 180 - RES - offset, lat_max
        bounds_end = -180 + offset, lat_min, lon_end, lat_max

        raster_start, transform, crs = crop_at_continous_lon_limits(
            bounds_start, datum=datum)
        raster_end, _, _ = crop_at_continous_lon_limits(
            bounds_end, datum=datum)

        raster = np.hstack([raster_start, raster_end])

    else:
        raster, transform, crs = crop_at_continous_lon_limits(
            bounds, datum=datum)
    return raster, transform, crs


def write_crop_to_file(array, transform, crs, path):
    """
    Write a georeferenced raster to a GeoTIFF file.

    Args:
        array (np.ndarray): raster array
        transform (affine.Affine): raster transform
        crs (rasterio.crs.CRS): raster CRS
        path (str): path to output file
    """
    height, width = array.shape
    profile = dict(driver="GTiff",
                   count=1,
                   width=width,
                   height=height,
                   dtype=array.dtype,
                   transform=transform,
                   crs=crs,
                   tiled=True,
                   compress="deflate",
                   predictor=2,
                   blockxsize=256,
                   blockysize=256)

    with rasterio.open(path, "w", **profile) as f:
        f.write(array, 1)