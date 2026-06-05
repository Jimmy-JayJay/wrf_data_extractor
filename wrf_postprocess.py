import os
import glob
import numpy as np
import pandas as pd
import xarray as xr
import matplotlib.pyplot as plt
import matplotlib.animation as animation
import geopandas as gpd
from scipy.spatial import cKDTree
from matplotlib.colors import LinearSegmentedColormap

# =============================================================================
# CONFIGURATION — Edit this block before each run
# =============================================================================

# Forecast window
FORECAST_START = "2026-06-01"
FORECAST_END   = "2026-06-06"

# Toggle outputs. Set to False to skip a slow step.
GENERATE_CSVS       = True
GENERATE_MAPS       = True
GENERATE_ANIMATION  = True

# Bounding box for your domain (decimal degrees)
LAT_MIN = -17.43
LAT_MAX =  -9.07
LON_MIN =  32.37
LON_MAX =  36.22

# File paths
STATIONS_FILE  = "stations.csv"        # see stations_example.csv for format
SHAPEFILE_PATH = "SHAPEFILE/admin.shp" # admin boundary shapefile for your domain

# WRF NetCDF file glob pattern — script expects exactly one match
NC_GLOB = "wrf_daily_*.nc"

# Stations known to be affected by a local surface bias (e.g. large lake, coast).
# Values are kept in output but flagged in the summary CSV.
# Add station names exactly as they appear in stations.csv.
BIAS_FLAGGED_STATIONS = set()

# Precipitation colormap (white → green → blue → purple).
# Follows the WMO standard bulletin palette; adjust to your organisation's style.
PRECIP_COLORS = [
    "#FFFFFF", "#E0F3DB", "#A8DDB5", "#4EB3D3",
    "#2B8CBE", "#0868AC", "#084081", "#4D004B"
]

# Colorbar caps
PRECIP_CAP_MM    = 50   # accumulated precip map (mm over full period)
PRECIP_CAP_DAILY = 30   # per-day animation frames (mm/day)

# Number of boundary cells to discard (removes WRF lateral boundary artefacts)
BOUNDARY_CELLS = 5

# =============================================================================
# HELPERS
# =============================================================================

def get_netcdf_filename():
    """Finds the WRF NetCDF file matching NC_GLOB. Raises if zero or many found."""
    files = glob.glob(NC_GLOB)
    if not files:
        raise FileNotFoundError(
            f"No NetCDF file found matching '{NC_GLOB}'. "
            "Check NC_GLOB in the configuration block."
        )
    if len(files) > 1:
        raise FileNotFoundError(
            f"Multiple NetCDF files found matching '{NC_GLOB}': {files}\n"
            "Keep exactly one file in the working directory per run."
        )
    return files[0]


def make_output_dir():
    """Creates a run-specific output directory and returns its path."""
    f_start = FORECAST_START.replace("-", "")
    f_end   = FORECAST_END.replace("-", "")
    out_dir = f"WRF_OUTPUT_{f_start}_{f_end}"
    os.makedirs(out_dir, exist_ok=True)
    print(f"Output directory: {out_dir}/")
    return out_dir


def out_path(output_dir, filename):
    return os.path.join(output_dir, filename)


# =============================================================================
# DATA LOADING & MASKING
# =============================================================================

def load_and_mask_data(nc_file):
    """
    Loads the dataset, slices to the forecast window, discards BOUNDARY_CELLS
    cells on each edge, and clips to the bounding box defined in configuration.
    """
    print(f"Loading {nc_file}...")
    ds = xr.open_dataset(nc_file, engine="netcdf4")

    ds = ds.sel(time=slice(FORECAST_START, FORECAST_END))
    time_len = len(ds.time)
    print(f"  {time_len} days extracted ({FORECAST_START} to {FORECAST_END})")

    y_len, x_len = ds.sizes["y"], ds.sizes["x"]
    b = BOUNDARY_CELLS
    ds = ds.isel(y=slice(b, y_len - b), x=slice(b, x_len - b))

    spatial_mask = (
        (ds.lat >= LAT_MIN) & (ds.lat <= LAT_MAX) &
        (ds.lon >= LON_MIN) & (ds.lon <= LON_MAX)
    )
    ds = ds.where(spatial_mask, drop=True)

    return ds


# =============================================================================
# STATION EXTRACTION
# =============================================================================

def extract_station_data(ds, stations_file):
    """
    Nearest-neighbour extraction for all stations using cKDTree.
    Returns a long-format DataFrame (one row per station per day).
    """
    stations_df = pd.read_csv(stations_file).dropna(subset=["LAT", "LON"])

    lat_flat = ds.lat.values.flatten()
    lon_flat = ds.lon.values.flatten()
    valid_idx = ~np.isnan(lat_flat)
    lat_valid = lat_flat[valid_idx]
    lon_valid = lon_flat[valid_idx]

    if len(lat_valid) == 0:
        raise ValueError(
            "No valid grid points inside the bounding box. "
            "Check LAT_MIN/MAX and LON_MIN/MAX in configuration."
        )

    tree = cKDTree(np.column_stack((lat_valid, lon_valid)))
    station_coords = np.column_stack(
        (stations_df["LAT"].values, stations_df["LON"].values)
    )
    _, indices = tree.query(station_coords)

    time_vals = pd.to_datetime(ds.time.values).strftime("%Y-%m-%d")
    n_times = len(time_vals)

    def _flat(var):
        return ds[var].values.reshape(n_times, -1)[:, valid_idx]

    tp_data        = _flat("tp")
    tmax_data      = _flat("tmax")
    tmin_data      = _flat("tmin")
    wind_mean_data = _flat("wind10_mean")
    wind_max_data  = _flat("wind10_max")

    records = []
    for i, row in stations_df.iterrows():
        ci = indices[i]
        is_flagged = row["station"].strip() in BIAS_FLAGGED_STATIONS
        for t_idx, t_date in enumerate(time_vals):
            def _r1(v):
                return round(float(v), 1) if not np.isnan(v) else np.nan
            def _r2(v):
                return round(float(v), 2) if not np.isnan(v) else np.nan

            wm = wind_mean_data[t_idx, ci]
            wx = wind_max_data[t_idx, ci]
            records.append({
                "station":          row["station"],
                "lat":              row["LAT"],
                "lon":              row["LON"],
                "date":             t_date,
                "tp_mm":            _r1(tp_data[t_idx, ci]),
                "tmax_degC":        _r1(tmax_data[t_idx, ci]),
                "tmin_degC":        _r1(tmin_data[t_idx, ci]),
                "wind_mean_ms":     _r2(wm),
                "wind_mean_kmh":    _r1(wm * 3.6) if not np.isnan(wm) else np.nan,
                "wind_max_ms":      _r2(wx),
                "wind_max_kmh":     _r1(wx * 3.6) if not np.isnan(wx) else np.nan,
                "bias_flag":        is_flagged,
            })

    return pd.DataFrame(records)


# =============================================================================
# CSV OUTPUTS
# =============================================================================

def generate_csv_outputs(df_long, ds, output_dir):
    """Writes four CSV formats into the run output directory."""
    f_start = FORECAST_START.replace("-", "")
    f_end   = FORECAST_END.replace("-", "")

    # 1. Daily long format
    df_long.to_csv(
        out_path(output_dir, f"wrf_stations_daily_{f_start}_{f_end}.csv"),
        index=False
    )

    # 2. Station summary (period aggregates)
    summary_rows = []
    for st in df_long["station"].unique():
        s = df_long[df_long["station"] == st]
        summary_rows.append({
            "station":              st,
            "lat":                  s["lat"].iloc[0],
            "lon":                  s["lon"].iloc[0],
            "bias_flag":            s["bias_flag"].iloc[0],
            "tp_total_mm":          round(s["tp_mm"].sum(), 1),
            "tmax_highest_degC":    round(s["tmax_degC"].max(), 1),
            "tmax_avg_degC":        round(s["tmax_degC"].mean(), 1),
            "tmin_lowest_degC":     round(s["tmin_degC"].min(), 1),
            "tmin_avg_degC":        round(s["tmin_degC"].mean(), 1),
            "wind_mean_avg_ms":     round(s["wind_mean_ms"].mean(), 2),
            "wind_mean_avg_kmh":    round(s["wind_mean_kmh"].mean(), 1),
            "wind_max_highest_ms":  round(s["wind_max_ms"].max(), 2),
            "wind_max_highest_kmh": round(s["wind_max_kmh"].max(), 1),
        })
    df_summary = pd.DataFrame(summary_rows)
    df_summary.to_csv(
        out_path(output_dir, f"wrf_stations_summary_{f_start}_{f_end}.csv"),
        index=False
    )

    # 3. Wide format (one row per station, daily columns + summary appended)
    df_wide = df_long.drop(columns=["bias_flag"]).pivot(
        index=["station", "lat", "lon"], columns="date"
    )
    df_wide.columns = [f"{col[0]}_{col[1]}" for col in df_wide.columns]
    df_wide = df_wide.reset_index()
    df_wide = pd.merge(
        df_wide,
        df_summary.drop(columns=["lat", "lon"]),
        on="station"
    )
    df_wide.to_csv(
        out_path(output_dir, f"wrf_stations_wide_{f_start}_{f_end}.csv"),
        index=False
    )

    # 4. Flattened grid (for use in downstream R or other map scripts)
    lat_flat  = ds.lat.values.flatten()
    lon_flat  = ds.lon.values.flatten()
    tp_grid   = ds["tp"].sum(dim="time").values.flatten()
    tmax_grid = ds["tmax"].max(dim="time").values.flatten()
    tmin_grid = ds["tmin"].min(dim="time").values.flatten()
    wind_grid = ds["wind10_mean"].mean(dim="time").values.flatten()

    valid = ~np.isnan(lat_flat) & ~np.isnan(tp_grid)
    df_grid = pd.DataFrame({
        "lat":               lat_flat[valid],
        "lon":               lon_flat[valid],
        "tp_total_mm":       np.round(tp_grid[valid], 1),
        "tmax_highest_degC": np.round(tmax_grid[valid], 1),
        "tmin_lowest_degC":  np.round(tmin_grid[valid], 1),
        "wind_mean_avg_ms":  np.round(wind_grid[valid], 2),
    })
    df_grid.to_csv(
        out_path(output_dir, f"wrf_grid_{f_start}_{f_end}.csv"),
        index=False
    )

    print("  4 CSV files written.")


# =============================================================================
# STATIC MAPS
# =============================================================================

def _base_map_axes(admin_boundaries, figsize=(7, 13), dpi=400):
    """Returns (fig, ax) with admin boundaries plotted."""
    fig, ax = plt.subplots(figsize=figsize, dpi=dpi, constrained_layout=True)
    admin_boundaries.plot(
        ax=ax, color="none", edgecolor="black", linewidth=0.5, zorder=3
    )
    ax.set_xlim(LON_MIN, LON_MAX)
    ax.set_ylim(LAT_MIN, LAT_MAX)
    ax.set_aspect("equal")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    return fig, ax


def generate_maps(ds, admin_boundaries, output_dir):
    """Generates accumulated precipitation and maximum temperature maps."""
    f_start = FORECAST_START.replace("-", "")
    f_end   = FORECAST_END.replace("-", "")
    n_days  = len(ds.time)

    cmap_precip = LinearSegmentedColormap.from_list("precip", PRECIP_COLORS)
    tp_accum  = ds["tp"].sum(dim="time")
    tmax_peak = ds["tmax"].max(dim="time")
    lats = ds.lat.values
    lons = ds.lon.values

    # Precipitation
    print("  Generating precipitation map...")
    fig, ax = _base_map_axes(admin_boundaries)
    pc = ax.pcolormesh(
        lons, lats, tp_accum.values,
        cmap=cmap_precip, vmin=0, vmax=PRECIP_CAP_MM,
        shading="gouraud", zorder=2
    )
    cbar = plt.colorbar(pc, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label("Accumulated Precipitation (mm)")
    ax.set_title(
        f"{n_days}-Day Accumulated Precipitation\n{FORECAST_START} to {FORECAST_END}"
    )
    fname = f"map_precip_{f_start}_{f_end}.png"
    plt.savefig(out_path(output_dir, fname), bbox_inches="tight")
    plt.close()
    print(f"    Saved {fname}")

    # Maximum temperature
    print("  Generating maximum temperature map...")
    fig, ax = _base_map_axes(admin_boundaries)
    pc = ax.pcolormesh(
        lons, lats, tmax_peak.values,
        cmap="RdYlBu_r", vmin=15, vmax=40,
        shading="gouraud", zorder=2
    )
    cbar = plt.colorbar(pc, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label("Maximum Temperature (°C)")
    ax.set_title(
        f"{n_days}-Day Maximum Temperature\n{FORECAST_START} to {FORECAST_END}"
    )
    fname = f"map_tmax_{f_start}_{f_end}.png"
    plt.savefig(out_path(output_dir, fname), bbox_inches="tight")
    plt.close()
    print(f"    Saved {fname}")


# =============================================================================
# ANIMATION
# =============================================================================

def generate_animation(ds, admin_boundaries, output_dir):
    """Creates a daily precipitation GIF, one frame per forecast day."""
    f_start = FORECAST_START.replace("-", "")
    f_end   = FORECAST_END.replace("-", "")

    cmap_precip = LinearSegmentedColormap.from_list("precip", PRECIP_COLORS)
    lats = ds.lat.values
    lons = ds.lon.values
    time_vals = pd.to_datetime(ds.time.values).strftime("%Y-%m-%d")
    tp_daily  = ds["tp"].values  # shape: (n_days, y, x)

    print("  Generating daily precipitation animation...")

    fig, ax = plt.subplots(figsize=(7, 13), dpi=150, constrained_layout=True)
    admin_boundaries.plot(
        ax=ax, color="none", edgecolor="black", linewidth=0.6, zorder=3
    )
    ax.set_xlim(LON_MIN, LON_MAX)
    ax.set_ylim(LAT_MIN, LAT_MAX)
    ax.set_aspect("equal")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")

    mesh = ax.pcolormesh(
        lons, lats, tp_daily[0],
        cmap=cmap_precip, vmin=0, vmax=PRECIP_CAP_DAILY,
        shading="gouraud", zorder=2
    )
    cbar = plt.colorbar(mesh, ax=ax, fraction=0.03, pad=0.04)
    cbar.set_label("Precipitation (mm/day)")
    title = ax.set_title(f"Daily Precipitation — {time_vals[0]}")

    def _update(frame_idx):
        mesh.set_array(tp_daily[frame_idx].ravel())
        title.set_text(f"Daily Precipitation — {time_vals[frame_idx]}")
        return mesh, title

    anim = animation.FuncAnimation(
        fig, _update,
        frames=len(time_vals),
        interval=800,
        blit=False
    )

    fname = f"anim_precip_daily_{f_start}_{f_end}.gif"
    anim.save(out_path(output_dir, fname), writer="pillow", fps=1.25)
    plt.close()
    print(f"    Saved {fname}")


# =============================================================================
# MAIN
# =============================================================================

def main():
    try:
        nc_file    = get_netcdf_filename()
        output_dir = make_output_dir()
        ds         = load_and_mask_data(nc_file)

        if GENERATE_CSVS:
            print("Extracting station data & writing CSVs...")
            df_long = extract_station_data(ds, STATIONS_FILE)
            generate_csv_outputs(df_long, ds, output_dir)

        if GENERATE_MAPS or GENERATE_ANIMATION:
            print("Loading shapefile...")
            admin_boundaries = gpd.read_file(SHAPEFILE_PATH)

        if GENERATE_MAPS:
            print("Generating static maps...")
            generate_maps(ds, admin_boundaries, output_dir)

        if GENERATE_ANIMATION:
            print("Generating animation...")
            generate_animation(ds, admin_boundaries, output_dir)

        print(f"\nDone. All outputs saved to: {output_dir}/")

    except Exception as e:
        import traceback
        print(f"\nERROR: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    main()
