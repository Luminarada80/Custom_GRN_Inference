import argparse
import logging
import os
from typing import Optional, Tuple, Union

import numpy as np
import pandas as pd
import pybedtools
from pybiomart import Server
from scipy.sparse import csr_matrix
import scanpy as sc
from anndata import AnnData

from grn_inference.plotting import plot_feature_score_histogram
from grn_inference.normalization import minmax_normalize_pandas
from grn_inference.utils import (
    load_atac_dataset,
    load_rna_dataset,
    find_genes_near_peaks,
    format_peaks
)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process TF motif binding potential.")
    parser.add_argument("--atac_data_file", type=str, required=True, help="Path to the scATAC-seq dataset")
    parser.add_argument("--rna_data_file", type=str, required=True, help="Path to the scRNA-seq dataset")
    parser.add_argument("--species", type=str, required=True, help="Species genome, e.g. 'mm10' or 'hg38'")
    parser.add_argument("--tss_distance_cutoff", type=int, required=False, default=1_000_000, help="Distance (bp) from TSS to filter peaks (default: 1,000,000)")
    parser.add_argument("--output_dir", type=str, required=True, help="Output directory for the sample")
    return parser.parse_args()

def is_normalized(df: pd.DataFrame, threshold: float = 1.5) -> bool:
    """
    Heuristically check if the dataset appears normalized.
    """
    values = df.iloc[:, 1:].values
    if not np.issubdtype(values.dtype, np.floating):
        return False
    mean_val = values[values > 0].mean()
    return 0 < mean_val < threshold

def log2_cpm_normalize(df: pd.DataFrame, id_col_name: str, label: str = "dataset") -> pd.DataFrame:
    if id_col_name not in df.columns:
        raise ValueError(f"Identifier column '{id_col_name}' not found in DataFrame.")

    # Split into ID column and numeric values
    id_col = df[[id_col_name]].reset_index(drop=True)
    counts = df.drop(columns=[id_col_name]).apply(pd.to_numeric, errors="coerce").fillna(0)

    # Combine and check normalization status
    full_df = pd.concat([id_col, counts], axis=1)
    if is_normalized(full_df):
        print(f" - {label} matrix appears already normalized. Skipping log2 CPM.", flush=True)
        return full_df

    print(f" - {label} matrix appears unnormalized. Applying log2 CPM normalization.", flush=True)

    # Compute log2 CPM
    library_sizes = counts.sum(axis=0)
    zero_lib = library_sizes == 0
    if zero_lib.any():
        logging.warning(f"Found {zero_lib.sum()} all-zero columns—setting library size to 1e-6 to avoid division by zero.")
        library_sizes[zero_lib] = 1e-6
    cpm = (counts.div(library_sizes, axis=1) * 1e6).add(1)
    log2_cpm = np.log2(cpm).reset_index(drop=True)

    return pd.concat([id_col, log2_cpm], axis=1)

def load_ensembl_organism_tss(organism, tmp_dir):
    if not os.path.exists(os.path.join(tmp_dir, "ensembl.parquet")):
        logging.info(f"Loading Ensembl TSS locations for {organism}")
        # Connect to the Ensembl BioMart server
        server = Server(host='http://www.ensembl.org')

        gene_ensembl_name = f'{organism}_gene_ensembl'
        
        # Select the Ensembl Mart dataset
        mart = server['ENSEMBL_MART_ENSEMBL']
        try:
            dataset = mart[gene_ensembl_name]
        except KeyError:
            raise RuntimeError(f"BioMart dataset {gene_ensembl_name} not found. Check if ‘{organism}’ is correct.")

        # Query for attributes: Ensembl gene ID, gene name, strand, and transcription start site (TSS)
        ensembl_df = dataset.query(attributes=[
            'external_gene_name', 
            'strand', 
            'chromosome_name',
            'transcription_start_site'
        ])

        ensembl_df.rename(columns={
            "Chromosome/scaffold name": "chr",
            "Transcription start site (TSS)": "tss",
            "Gene name": "gene_id"
        }, inplace=True)
        
        # Make sure TSS is integer (some might be floats).
        ensembl_df["tss"] = ensembl_df["tss"].astype(int)

        # In a BED file, we’ll store TSS as [start, end) = [tss, tss+1)
        ensembl_df["start"] = ensembl_df["tss"].astype(int)
        ensembl_df["end"] = ensembl_df["tss"].astype(int) + 1

        # Re-order columns for clarity: [chr, start, end, gene]
        ensembl_df = ensembl_df[["chr", "start", "end", "gene_id"]]
        
        ensembl_df["chr"] = ensembl_df["chr"].astype(str)
        ensembl_df["gene_id"] = ensembl_df["gene_id"].astype(str)
        
        # Write the peak DataFrame to a file
        ensembl_df.to_parquet(os.path.join(tmp_dir, "ensembl.parquet"), engine="pyarrow", index=False, compression="snappy")
        
    else:
        logging.info("Ensembl gene TSS BED file exists, loading...")
        ensembl_df = pd.read_parquet(os.path.join(tmp_dir, "ensembl.parquet"), engine="pyarrow")
    
    return ensembl_df

def calculate_tss_distance_score(
    peak_bed: pybedtools.BedTool, 
    tss_bed: pybedtools.BedTool, 
    gene_names: set[str], 
    tss_distance_cutoff: Union[int, float] = 1e6
    ):
    """
    Identify genes whose transcription start sites (TSS) are near scATAC-seq peaks.
    
    This function:
        1. Calculates the absolute distances between peaks and gene TSSs within `tss_distance_cutoff`.
        2. Scales peak to gene distances using an exponential drop-off function (e^-dist/250000),
           the same method used in the LINGER cis-regulatory potential calculation.
        3. Deduplicates the data to keep the minimum (i.e., best) peak-to-gene connection.
        4. Only keeps genes that are present in the RNA-seq dataset.
        
    Args:
        peak_bed (pybedtools.BedTool):
            BedTool object representing scATAC-seq peaks.
        tss_bed (pybedtools.BedTool):
            BedTool object representing gene TSS locations.
        gene_names (set[str]):
            Set of gene names from the scRNA-seq dataset.
        tss_distance_cutoff (int): 
            The maximum distance (in bp) from a TSS to consider a peak as potentially regulatory.
        
    Returns:
        peak_tss_subset_df (pandas.DataFrame): 
            A DataFrame containing columns "peak_id", "target_id", and the scaled TSS distance "TSS_dist"
            for peak–gene pairs.
    """
    peak_tss_overlap_df = find_genes_near_peaks(
        peak_bed, tss_bed, tss_distance_cutoff
        )

    # Scale the TSS distance using an exponential drop-off function
    # e^-dist/25000, same scaling function used in LINGER Cis-regulatory potential calculation
    # https://github.com/Durenlab/LINGER
    peak_tss_overlap_df["TSS_dist_score"] = np.exp(-peak_tss_overlap_df["TSS_dist"] / 250000)
    
    # Keep only the necessary columns.
    peak_tss_subset_df: pd.DataFrame = peak_tss_overlap_df[["peak_id", "target_id", "TSS_dist_score"]]
    
    # Filter out any genes not found in the RNA-seq dataset.
    gene_names_upper = set(g.upper() for g in gene_names)
    mask = peak_tss_subset_df["target_id"].str.upper().isin(gene_names_upper)
    peak_tss_subset_df = peak_tss_subset_df[mask]
    
    logging.info(f'\t- Number of peaks: {len(peak_tss_subset_df.drop_duplicates(subset="peak_id"))}')
    
    return peak_tss_subset_df
    
def extract_atac_peaks_near_rna_genes(
    atac_df: pd.DataFrame, 
    rna_df: pd.DataFrame, 
    organism: str, 
    tss_distance_cutoff: int, 
    output_dir: str
    ) -> pd.DataFrame:
    
    tmp_dir = os.path.join(output_dir, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    
    if organism not in ("hsapiens", "mmusculus"):
        if organism == "hg38":
            organism = "hsapiens"
        elif organism == "mm10":
            organism = "mmusculus"
        else:
            raise ValueError(f"Organism not recognized: {organism} (must be 'hg38' or 'mm10').")

    # Format the ATAC peaks in BED format or load a cached file
    if not os.path.exists(f"{tmp_dir}/peak_df.parquet"):
        logging.info(f"Extracting peak information and saving as a bed file")
        
        peak_df = format_peaks(atac_df["peak_id"])
    
        # Set the peak location dataframe to the correct BED format
        bedtool_df = peak_df.rename(columns={
            "chromosome": "chrom",
            "peak_id": "name"
        })[["chrom", "start", "end", "name", "strand"]]
    
    else:
        logging.info("ATAC-seq BED file exists, loading...")
        peak_df = pd.read_parquet(os.path.join(tmp_dir, "peak_df.parquet"), engine="pyarrow")
        
        assert list(peak_df.columns) == ["chrom", "start", "end", "name", "strand"], \
            "peak_df must have columns: chrom, start, end, name, strand"
            
    if not os.path.exists(os.path.join(output_dir, "tss_distance_score.parquet")):
    
        tss_df: pd.DataFrame = load_ensembl_organism_tss(organism, tmp_dir)

        pybedtools.set_tempdir(tmp_dir)
        try:
            peak_bed = pybedtools.BedTool.from_dataframe(bedtool_df)
            tss_bed = pybedtools.BedTool.from_dataframe(tss_df)
            
            assert os.path.isdir(output_dir), "`output_dir` is not a directory"

            peak_tss_subset_df = calculate_tss_distance_score(
                peak_bed,
                tss_bed,
                gene_names,
                tss_distance_cutoff
            )
            
            peak_tss_subset_df = minmax_normalize_pandas(
                df=peak_tss_subset_df, 
                score_cols=["TSS_dist_score"], 
                )
                
            peak_tss_subset_df.to_parquet(os.path.join(output_dir, "tss_distance_score.parquet"), index=False, engine="pyarrow", compression="snappy")
        finally:
            pybedtools.helpers.cleanup(verbose=False, remove_all=True) 
    else:
        logging.info('TSS distance file exists, loading...')
        peak_tss_subset_df = pd.read_parquet(os.path.join(output_dir, "tss_distance_score.parquet"), engine="pyarrow")
    

    pybedtools.helpers.cleanup(verbose=False, remove_all=True)
    
    plot_feature_score_histogram(peak_tss_subset_df, "TSS_dist_score", output_dir)
    
    return peak_tss_subset_df

def ensure_matching_cell_barcodes(atac_df, rna_df):
    atac_cells = atac_df.columns[1:]
    rna_cells = rna_df.columns[1:]
    
    if len(atac_cells) == 0 or len(rna_cells) == 0:
        raise RuntimeError("No cell columns found in one or both datasets.")
    
    num_matching_cells = len(atac_cells.intersection(rna_cells))
    
    atac_percent_overlap = num_matching_cells / len(atac_cells)
    rna_percent_overlap = num_matching_cells / len(rna_cells)
    
    if atac_percent_overlap <= 0.1:
        raise ValueError(f"Too few matching barcodes in ATAC (only {atac_percent_overlap*100:.2f}%).")
    if rna_percent_overlap <= 0.1:
        raise ValueError(f"Too few matching barcodes in RNA (only {rna_percent_overlap*100:.2f}%).")

def convert_anndata_to_pandas(adata: AnnData, id_col_name: str) -> pd.DataFrame:
    """
    Convert an AnnData object to a Pandas DataFrame.
    
    - **var_names** = gene / peak names
    - **obs_names** = cell names / barcodes

    Args:
        adata (AnnData): AnnData object containing scATAC-seq or scRNA-seq data
        id_col_name (str): Name for the peak / gene ID column ("peak_id" or "gene_id")

    Returns:
        pd.DataFrame: DataFrame of gene x cell expression data. Header contains cell names, column 0 
        contains gene / peak names
    """
    adata = adata.copy()
    
    df = pd.DataFrame(
        data=adata.X.T.toarray(),
        index=adata.var_names,    # genes
        columns=adata.obs_names    # filtered cells
    )
    
    # Add the gene / peak names as column 0 rather than the index
    df.insert(loc=0, column=id_col_name, value=df.index.astype(str))
    df = df.reset_index(drop=True)

    return df
    
def filter_rna_seq_dataset(
    rna_df: pd.DataFrame,
    id_col_name: str = "gene_id",
    min_genes: int = 200,
    max_genes: int = 2500,
    max_pct_mt: float = 5.0,
) -> pd.DataFrame:
    """
    Given an RNA‐seq DataFrame in which rows are genes and columns are cells,
    automatically filter cells based on:
      - number of genes detected (min_genes <= n_genes_by_counts <= max_genes)
      - mitochondrial percentage (pct_counts_mt < max_pct_mt)
    """
    # 1) Validate input
    if id_col_name not in rna_df.columns:
        raise ValueError(f"Identifier column '{id_col_name}' not found in DataFrame.")

    # Separate gene IDs vs. raw count matrix
    gene_ids = rna_df[id_col_name].astype(str).tolist()
    counts_df = rna_df.drop(columns=[id_col_name]).copy()

    # Ensure all other columns are numeric; coerce non‐numeric to NaN→0
    counts_df = counts_df.apply(pd.to_numeric, errors="coerce").fillna(0).astype(int)

    # Extract cell IDs from the DataFrame columns
    cell_ids = counts_df.columns.astype(str).tolist()

    # 2) Build AnnData with shape (cells × genes)
    #    We must transpose counts so rows=cells, columns=genes
    counts_matrix = csr_matrix(counts_df.values)           # shape: (n_genes, n_cells)
    counts_matrix = counts_matrix.T                         # now (n_cells, n_genes)

    adata = AnnData(X=counts_matrix)
    adata.obs_names = cell_ids       # each row = one cell
    adata.var_names = gene_ids       # each column = one gene

    # 3) Annotate var (genes) with flags: mt, ribo, hb
    #    Mitochondrial: genes whose name starts with "MT-" (case-insensitive).
    #    Ribosomal:     genes whose name starts with "RPS" or "RPL".
    #    Hemoglobin:    genes whose name matches "^HB(?!P)" (so HB* but not HBP).
    var_names_lower = adata.var_names.str.lower()

    adata.var["mt"] = var_names_lower.str.startswith("mt-")
    adata.var["ribo"] = var_names_lower.str.startswith(("rps", "rpl"))
    adata.var["hb"] = var_names_lower.str.contains(r"^hb(?!p)")

    sc.pp.calculate_qc_metrics(
        adata,
        qc_vars=["mt", "ribo", "hb"],
        percent_top=None,
        log1p=False,
        inplace=True,
    )
    
    cell_mask = (
        (adata.obs["n_genes_by_counts"] >= min_genes)
        & (adata.obs["n_genes_by_counts"] <= max_genes)
        & (adata.obs["pct_counts_mt"] < max_pct_mt)
    )

    n_before = adata.n_obs
    n_after = cell_mask.sum()
    logging.info(f"Cells before filtering: {n_before}")
    logging.info(f"Cells after filtering : {n_after}  (kept those with {min_genes} ≤ n_genes ≤ {max_genes} and pct_counts_mt < {max_pct_mt}%)")

    if n_after == 0:
        raise RuntimeError("No cells passed the filtering criteria. "
                           "Check `min_genes`, `max_genes`, `max_pct_mt` settings.")

    filtered_adata = adata[cell_mask].copy()

    filtered_df = convert_anndata_to_pandas(filtered_adata, id_col_name)

    return filtered_df

def filter_atac_seq_dataset(
    atac_df: pd.DataFrame,
    id_col_name: str = "peak_id",
    min_peaks: int = 2000,
    max_peaks: int = 40000,
    ) -> pd.DataFrame:
    
    df = atac_df.copy()
    
    df = df.set_index("peak_id")
    counts = csr_matrix(df.values)
    counts = counts.T
    peak_names = df.index.to_list()
    cell_names = df.columns.to_list()
    
    adata = AnnData(counts)
    adata.obs_names = cell_names
    adata.var_names = peak_names
    
    adata.obs["n_peaks"] = np.array((adata.X > 0).sum(axis=1)).ravel()
    
    keep_cells = (
        (adata.obs["n_peaks"] > min_peaks) &
        (adata.obs["n_peaks"] < max_peaks)
    )
    
    filtered_adata = adata[keep_cells]
    
    n_before = adata.n_obs
    n_after = keep_cells.sum()
    
    logging.info(f"Cells before filtering: {n_before}")
    logging.info(f"Cells after filtering : {n_after}  (kept those with {min_peaks} ≤ n_genes ≤ {max_peaks}")

    
    filtered_counts = filtered_adata.X.T.toarray()
    filtered_df = pd.DataFrame(
        data=filtered_counts,
        index=filtered_adata.var_names,    # genes
        columns=filtered_adata.obs_names    # filtered cells
    )
    filtered_df.insert(loc=0, column=id_col_name, value=filtered_df.index.astype(str))
    filtered_df = filtered_df.reset_index(drop=True)
    return filtered_df

def main(args):
    args.atac_data_file = os.path.abspath(args.atac_data_file)
    args.rna_data_file = os.path.abspath(args.rna_data_file)
    output_dir = os.path.abspath(args.output_dir)
    
    if not os.path.isfile(args.atac_data_file):
        raise FileNotFoundError(f"ATAC file not found: {args.atac_data_file}")

    if not os.path.isfile(args.rna_data_file):
        raise FileNotFoundError(f"RNA file not found: {args.rna_data_file}")
    
    logging.info("\nLoading ATAC-seq dataset")
    raw_atac_df: pd.DataFrame = load_atac_dataset(args.atac_data_file)

    logging.info("\nLoading RNA-seq dataset")
    raw_rna_df: pd.DataFrame = load_rna_dataset(args.rna_data_file)
    
    logging.info("\nEnsuring that the cell barcodes match for the ATACseq and RNAseq datasets")
    ensure_matching_cell_barcodes(raw_atac_df, raw_rna_df)
    
    logging.info("\nFiltering the scRNAseq dataset")
    rna_df = filter_rna_seq_dataset(
        raw_rna_df,
        id_col_name="gene_id",
        min_genes=200,
        max_genes=2500,
        max_pct_mt=5.0,
    )
    
    logging.info("\nFiltering the scATACseq dataset")
    atac_df = filter_atac_seq_dataset(
        raw_atac_df,
        id_col_name="peak_id",
        min_peaks=2000,
        max_peaks=40000,
    )
    
    logging.info("\nChecking and normalizing ATAC-seq data")
    atac_df_norm: pd.DataFrame = log2_cpm_normalize(atac_df, label="scATAC-seq", id_col_name="peak_id")

    logging.info("Checking and normalizing RNA-seq data")
    rna_df_norm: pd.DataFrame = log2_cpm_normalize(rna_df, label="scRNA-seq", id_col_name="gene_id")

    logging.info("\nExtracting ATAC peaks within 1 MB of a gene from the RNA dataset")
    peaks_near_genes_df = extract_atac_peaks_near_rna_genes(atac_df, rna_df, args.species, args.tss_distance_cutoff, output_dir)
                
    logging.info("\nFiltering for peaks with 1MB of a gene's TSS")
    peak_subset = set(peaks_near_genes_df["peak_id"])
    atac_df_filtered = atac_df_norm[atac_df_norm["peak_id"].isin(peak_subset)]
    logging.info(f'\tNumber of peaks after filtering: {len(atac_df_filtered)} / {len(atac_df_norm)}')
    
    logging.info("\nFiltering for genes with a TSS within 1 MB of a peak")
    genes_subset = set(peaks_near_genes_df["target_id"])
    rna_df_filtered = rna_df_norm[rna_df_norm["gene_id"].isin(genes_subset)]
    logging.info(f'\tNumber of genes after filtering: {len(rna_df_filtered)} / {len(rna_df_norm)}')
    
    logging.info("\nOnly keeping cells that are in both the RNAseq and ATACseq filtered datasets")
    atac_cells = set(atac_df_filtered.columns) - {"peak_id"}
    rna_cells = set(rna_df_filtered.columns) - {"gene_id"}

    common_cells = sorted(rna_cells.intersection(atac_cells))

    final_atac = atac_df_filtered[["peak_id"] + common_cells].copy()
    final_rna = rna_df_filtered[["gene_id"] + common_cells].copy()
    

    # 4) (Optional) Log how many cells remain
    print(f"Number of cells in RNA after intersect: {len(common_cells)}")
    print(f"Number of cells in ATAC after intersect: {len(common_cells)}")
    
    logging.info(f"\nProcessed scATACseq dataset:")
    logging.info(f"  - Peaks: {final_atac.shape[0]:,}")
    logging.info(f"  - Cells: {final_atac.shape[1]:,}")
    
    logging.info(f"\nProcessed scRNAseq dataset:")
    logging.info(f"  - Genes: {final_rna.shape[0]:,}")
    logging.info(f"  - Cells: {final_rna.shape[1]:,}")
    
    def update_name(filename):
        base, ext = os.path.splitext(filename)
        return f"{base}_processed.parquet"

    new_atac_file = update_name(args.atac_data_file)
    new_rna_file = update_name(args.rna_data_file)

    print(f"\nUpdated ATAC file: {new_atac_file}", flush=True)
    print(f"Updated RNA file: {new_rna_file}", flush=True)
    
    logging.info('\nWriting ATAC-seq dataset to Parquet')
    final_atac.to_parquet(new_atac_file, engine="pyarrow", compression="snappy", index=False)
    logging.info("  Done!")
        
    logging.info('\nWriting RNA-seq dataset to Parquet')
    final_rna.to_parquet(new_rna_file, engine="pyarrow", compression="snappy", index=False)
    logging.info("  Done!")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    args = parse_args()
    
    main(args)
