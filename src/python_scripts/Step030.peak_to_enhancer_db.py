import pandas as pd
import dask.dataframe as dd
import logging
import argparse
import pybedtools
import os

def parse_args() -> argparse.Namespace:
    """
    Parses command-line arguments.

    Returns:
    argparse.Namespace: Parsed arguments containing paths for input and output files.
    """

    parser = argparse.ArgumentParser(description="Process TF motif binding potential.")
    
    parser.add_argument(
        "--atac_data_file",
        type=str,
        required=True,
        help="Path to the scATACseq data file"
    )
    parser.add_argument(
        "--enhancer_db_file",
        type=str,
        required=True,
        help="Path to the EnhancerDB file"
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        required=True,
        help="Path to the output directory for the sample"
    )
    
    args: argparse.Namespace = parser.parse_args()

    return args

def extract_atac_peaks(atac_df, tmp_dir):
    peak_pos = atac_df["peak_id"].tolist()

    peak_df = pd.DataFrame()
    peak_df["chr"] = [pos.split(":")[0].replace("chr", "") for pos in peak_pos]
    peak_df["start"] = [int(pos.split(":")[1].split("-")[0]) for pos in peak_pos]
    peak_df["end"] = [int(pos.split(":")[1].split("-")[1]) for pos in peak_pos]
    peak_df["peak_id"] = peak_pos

    peak_df["chr"] = peak_df["chr"].astype(str)
    peak_df["start"] = peak_df["start"].astype(int)
    peak_df["end"] = peak_df["end"].astype(int)
    peak_df["peak_id"] = peak_df["peak_id"].astype(str)
    
    # Write the peak DataFrame to a file
    peak_df.to_csv(f"{tmp_dir}/peak_df.bed", sep="\t", header=False, index=False)

def load_enhancer_database_file(enhancer_db_file, tmp_dir):
    enhancer_db = pd.read_parquet(enhancer_db_file)
    enhancer_db = enhancer_db.rename(columns={
        0 : "chr",
        1 : "start",
        2 : "end",
        3 : "enhancer",
        4 : "tissue",
        5 : "R1_value",
        6 : "R2_value",
        7 : "R3_value",
        8 : "score"
    })
    
    # Remove the "chr" before chromosome number
    enhancer_db["chr"] = enhancer_db["chr"].str.replace("^chr", "", regex=True)
    
    # Average the score of an enhancer across all tissues / cell types
    enhancer_db = enhancer_db.groupby(["chr", "start", "end", "enhancer"], as_index=False)["score"].mean()

    enhancer_db["chr"] = enhancer_db["chr"].astype(str)
    enhancer_db["start"] = enhancer_db["start"].astype(int)
    enhancer_db["end"] = enhancer_db["end"].astype(int)
    enhancer_db["enhancer"] = enhancer_db["enhancer"].astype(str)
    
    enhancer_db = enhancer_db[["chr", "start", "end", "enhancer", "score"]]
    
    # Write the peak DataFrame to a file
    enhancer_db.to_csv(f"{tmp_dir}/enhancer.bed", sep="\t", header=False, index=False)

def find_peaks_in_known_enhancer_region(peak_bed, enh_bed):
    # 4) Find peaks that overlap with known enhancer locations from EnhancerDB
    logging.info("Locating peaks that overlap with known enhancer locations from EnhancerDB")
    peak_enh_overlap = peak_bed.intersect(enh_bed, wa=True, wb=True)
    peak_enh_overlap_df = peak_enh_overlap.to_dataframe(
        names=[
            "peak_chr", "peak_start", "peak_end", "peak_id",
            "enh_chr", "enh_start", "enh_end", "enh_id",
            "enh_score"  # only if you had a score column in your enhancers
        ]
    ).dropna()
    peak_enh_overlap_subset_df = peak_enh_overlap_df[["peak_id", "enh_score"]]
        
    return peak_enh_overlap_subset_df

def main():
    # ============ MAPPING PEAKS TO KNOWN ENHANCERS ============
    # Checks to make sure the peak_df.bed file exists, or else creates it 
    # (This file should be created during Step020.peak_gene_correlation.py)
    if not os.path.exists(f"{TMP_DIR}/peak_df.bed"):
        logging.info("Loading and parsing the ATAC-seq peaks")
        atac_df: pd.DataFrame = pd.read_parquet(ATAC_DATA_FILE)
        
        logging.info(f"Extracting peak information and saving as a bed file")
        extract_atac_peaks(atac_df, TMP_DIR)
    else:
        logging.info("ATAC-seq BED file exists, loading...")

    # Dataframe with "peak_id", "enh_id", and "enh_score" columns
    if not os.path.exists(f"{TMP_DIR}/enhancer.bed"):
        logging.info("Loading known enhancer locations from EnhancerDB and saving as a bed file")
        load_enhancer_database_file(ENHANCER_DB_FILE, TMP_DIR)
    else:
        logging.info("Enhancer BED file exists, loading...")

    # Load the peak and gene TSS BED files
    peak_df = dd.read_parquet(f"{TMP_DIR}/peak_df.parquet").compute()
    enh_df = dd.read_parquet(f"{TMP_DIR}/enh_df.parquet").compute()

    peak_bed = pybedtools.BedTool.from_dataframe(peak_df)
    enh_bed = pybedtools.BedTool.from_dataframe(enh_df)

    # Find the peaks that are in known enhancer regions
    peak_enh_df = find_peaks_in_known_enhancer_region(peak_bed, enh_bed)

    # Write out the final dataframe to the output directory
    peak_enh_df.to_parquet(f'{OUTPUT_DIR}/peak_to_known_enhancers.parquet', engine="pyarrow", index=False, compression="snappy")

if __name__ == "__main__":
    # Configure logging
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    # Parse arguments
    args: argparse.Namespace = parse_args()

    ATAC_DATA_FILE = args.atac_data_file
    ENHANCER_DB_FILE = args.enhancer_db_file
    OUTPUT_DIR = args.output_dir
    TMP_DIR = f"{OUTPUT_DIR}/tmp"
    
    main()