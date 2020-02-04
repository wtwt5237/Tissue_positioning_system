import argparse
from goz.find_zones import *
from goz.plotting import *
from goz.segmentation import *
import os
from skimage import io
import skimage as ski
import pandas as pd
import warnings
import matplotlib

warnings.filterwarnings("ignore")

if __name__ == "__main__":
    matplotlib.use("Agg")
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Worker script for Autozone",
    )
    parser.add_argument(
        "input_img",
        type=str,
        help="Absolute Input TIF image to be zonated, with signal of interest at channel 0, \
                            GS at channel 1 and DAPI at channel 2",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        nargs="?",
        default="",
        help="output folder of results, if not supplied, it will be that same as the input file name.",
    )
    parser.add_argument(
        "-v",
        "--vessel_size_factor",
        type=int,
        nargs="?",
        default=2,
        help="Vessel size threshold as x/10000 fold of image size",
    )
    parser.add_argument(
        "-d",
        "--maximal_neighbor_distance",
        type=int,
        nargs="?",
        default=20,
        help="maximal pixel distance between two neighboring masks to be considered as two separate masks.",
    )
    parser.add_argument(
        "-c",
        "--dapi_cutoff",
        type=int,
        nargs="?",
        default=20,
        help="Dapi cutoff value for hard thresholding.",
    )
    parser.add_argument(
        "-gl",
        "--gs_lower_limit",
        type=float,
        nargs="?",
        default=0.25,
        help="The lower percentatge limit of GS signal intensity within a mask, which is used in classify CV from PV",
    )
    parser.add_argument(
        "-gh",
        "--gs_higher_limit",
        type=float,
        nargs="?",
        default=0.75,
        help="The higher percentatge limit of GS signal intensity within a mask, which is used in classify CV from PV",
    )
    parser.add_argument(
        "-gs",
        "--gs_step",
        type=float,
        nargs="?",
        default=0.1,
        help="The interval of percentage in the GS intensity features.",
    )
    parser.add_argument(
        "-s",
        "--spot_size",
        type=bool,
        nargs="?",
        default=False,
        help="If zonal spot sizes are calculated. This will only work for sparse signals.",
    )
    parser.add_argument(
        "-u",
        "--update",
        type=bool,
        nargs="?",
        default=False,
        help="Check for existing analysis results, if exist, skip the job.",
    )
    # Parse all arguments
    args = parser.parse_args()
    input_tif_fn = args.input_img
    output = args.output
    vessel_size_factor = args.vessel_size_factor
    max_dist = args.maximal_neighbor_distance
    gs_low = args.gs_lower_limit
    gs_high = args.gs_higher_limit
    gs_step = args.gs_step
    update = args.update
    dapi_cutoff = args.dapi_cutoff
    spot_size = args.spot_size

    output_prefix = input_tif_fn.replace(".tif", "/")
    if output != "":
        output_prefix = os.path.join(output, output_prefix.split("/")[-2], "")
    output_mask_fn = output_prefix + "masks.tif"
    print("Prosessing {}".format(input_tif_fn))
    print("Parameters: {}".format(args))
    img = io.imread(input_tif_fn)
    if not os.path.exists(output_prefix):
        os.mkdir(output_prefix)
    if os.path.exists(output_prefix + "zone int.csv") & (not update):
        print("Analysis already done, skip this job.")
    else:
        if os.path.exists(output_mask_fn):
            print("Use existing masks")
            masks = io.imread(output_mask_fn)
            _, _, gs_ica = extract_gs_channel(img)
            vessels = segmenting_vessels(
                img, dilation_t=0, dark_t=dapi_cutoff, dapi_channel=2, vessel_size_t=2
            )
            print("Merging neighboring vessel masks...")
            vessels = ski.measure.label(vessels, connectivity=2)
            new_merged_mask, _ = merge_neighboring_vessels(vessels, max_dist=max_dist)
            while not (new_merged_mask == vessels).all():
                vessels = new_merged_mask
                print("Continue merging neighboring masks...")
                new_merged_mask, _ = merge_neighboring_vessels(
                    vessels, max_dist=max_dist
                )
            vessels = new_merged_mask
        else:
            print("Segmentating using GS and DAPI")
            try:
                masks, gs_ica, vessels = segmenting_vessels_gs_assisted(
                    img,
                    vessel_size_t=vessel_size_factor,
                    max_dist=max_dist,
                    dark_t=dapi_cutoff,
                )
            except:
                print(
                    "Default DAPI cutoff failed, try using 0.5 * Otsu threshold values."
                )
                dapi_cutoff = 0.5 * ski.filters.threshold_otsu(img[:, :, 2])
                masks, gs_ica, vessels = segmenting_vessels_gs_assisted(
                    img,
                    vessel_size_t=vessel_size_factor,
                    max_dist=max_dist,
                    dark_t=dapi_cutoff,
                )
            # Save masks
            io.imsave(output_mask_fn, masks.astype(np.uint8))
        # get CV PV classification

        cv_features = extract_features(
            masks, gs_ica, q1=gs_low, q2=gs_high, step=gs_step
        )
        cv_labels, pv_labels = pv_classifier(cv_features.loc[:, "I0":], masks)

        # modify CV masks to shrink their borders
        cv_masks = masks * np.isin(masks, cv_labels).copy()
        pv_masks = masks * np.isin(masks, pv_labels).copy()
        masks = shrink_cv_masks(cv_masks, pv_masks, vessels)
        cv_masks = masks * np.isin(masks, cv_labels).copy()
        pv_masks = masks * np.isin(masks, pv_labels).copy()
        plot_pv_cv(masks, cv_labels, img, output_prefix + "Marker ")

        # find lobules
        cv_masks = cv_masks.astype('uint8')
        _, lobules_sizes, lobule_edges = find_lobules(
            cv_masks, lobule_name=output_prefix.replace(".tif", "")
        )
        lobules_sizes.to_csv(output_prefix + "lobule_sizes.csv")
        plot3channels(
            lobule_edges,
            cv_masks != 0,
            pv_masks != 0,
            fig_name=output_prefix + "lobules",
        )

        # Calculate distance projections
        #! orphan cut off set at 550
        zone_crit = calculate_zone_crit(cv_masks, pv_masks, tolerance=550)

        # Calculate zones
        zones = create_zones(
            masks,
            zone_crit,
            cv_labels,
            pv_labels,
            zone_break_type="equal_quantile",
            num_zones=24,
        )

        # Plot zones with image
        plot_zone_with_img(img, zones, fig_prefix=output_prefix + "zones with marker")
        plot_zones_only(zones, fig_prefix=output_prefix + "zones only")
        # Calculate zonal spot sizes.
        if spot_size:
            _ = get_zonal_spot_sizes(img[:, :, 0], zones, output_prefix)
        # Calculate zonal reporter expression levels.
        zone_int = plot_zone_int_probs(
            img[:, :, 0],
            img[:, :, 2],
            zones,
            dapi_cutoff="otsu",
            plot_type="probs",
            prefix=output_prefix + "Marker",
        )
        zone_int.to_csv(output_prefix + "zone int.csv")

