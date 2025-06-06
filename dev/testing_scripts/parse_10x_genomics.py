import pandas as pd
import scipy
import anndata
from scipy.sparse import  csc_matrix, coo_matrix
import scanpy as sc
import numpy as np
import os
import logging
import pyranges as pr

# From Duren Lab's LINGER.preprocessing
def get_adata(matrix: csc_matrix, features: pd.DataFrame, barcodes: pd.DataFrame):
    """
    Processes input RNA and ATAC-seq data to generate AnnData objects for RNA and ATAC data, 
    filters by quality, aligns by barcodes, and adds cell-type labels.

    Parameters:
        matrix (csc_matrix):
            A sparse matrix (CSC format) containing gene expression or ATAC-seq data where rows are features 
            (genes/peaks) and columns are cell barcodes.
        features (pd.DataFrame):
            A DataFrame containing information about the features. 
            Column 1 holds the feature names (e.g., gene IDs or peak names), and column 2 categorizes features 
            as "Gene Expression" or "Peaks".
        barcodes (pd.DataFrame):
            A DataFrame with one column of cell barcodes corresponding to the columns of the matrix.
        label (pd.DataFrame):
            A DataFrame containing cell-type annotations with the columns 'barcode_use' (for cell barcodes) 
            and 'label' (for cell types).

    Returns:
        tuple[AnnData, AnnData]:
            A tuple containing the filtered and processed AnnData objects for RNA and ATAC data.
            1. `adata_RNA`: The processed RNA-seq data.
            2. `adata_ATAC`: The processed ATAC-seq data.
    """

    # Ensure matrix data is in float32 format for memory efficiency and consistency
    matrix.data = matrix.data.astype(np.float32)

    # Create an AnnData object with the transposed matrix (cells as rows, features as columns)
    adata = anndata.AnnData(X=csc_matrix(matrix).T)
    logging.info(f'Reading 10X genomics dataset with {adata.shape[0]:,} cells and {adata.shape[1]:,} features')
    
    # Assign feature names (e.g., gene IDs or peak names) to the variable (features) metadata in AnnData
    adata.var['gene_ids'] = features[1].values
    
    # Assign cell barcodes to the observation (cells) metadata in AnnData
    adata.obs['barcode'] = barcodes[0].values

    # Check if barcodes contain sample identifiers (suffix separated by '-'). If so, extract the sample number
    if len(barcodes[0].values[0].split("-")) == 2:
        adata.obs['sample'] = [int(string.split("-")[1]) for string in barcodes[0].values]
    else:
        # If no sample suffix, assign all cells to sample 1
        adata.obs['sample'] = 1

    # Subset features based on their type (Gene Expression or Peaks)
    # Select rows corresponding to "Gene Expression"
    rows_to_select: pd.Index = features[features[2] == 'Gene Expression'].index
    adata_RNA = adata[:, rows_to_select]

    # Select rows corresponding to "Peaks"
    rows_to_select = features[features[2] == 'Peaks'].index
    adata_ATAC = adata[:, rows_to_select]

    ### If cell-type label (annotation) is provided, filter and annotate AnnData objects based on the label

    # # Filter RNA and ATAC data to keep only the barcodes present in the label
    # idx: pd.Series = adata_RNA.obs['barcode'].isin(label['barcode_use'].values)
    # adata_RNA = adata_RNA[idx]
    # adata_ATAC = adata_ATAC[idx]

    # # Set the index of the label DataFrame to the barcodes
    # label.index = label['barcode_use']

    # # Annotate cell types (labels) in the RNA data
    # adata_RNA.obs['label'] = label.loc[adata_RNA.obs['barcode']]['label'].values

    # # Annotate cell types (labels) in the ATAC data
    # adata_ATAC.obs['label'] = label.loc[adata_ATAC.obs['barcode']]['label'].values

    ### Quality control filtering on the RNA data
    # Identify mitochondrial genes (which start with "MT-")
    adata_RNA.var["mt"] = adata_RNA.var_names.str.startswith("MT-")

    # Calculate QC metrics, including the percentage of mitochondrial gene counts
    sc.pp.calculate_qc_metrics(adata_RNA, qc_vars=["mt"], percent_top=None, log1p=False, inplace=True)

    # Filter out cells with more than 5% of counts from mitochondrial genes
    adata_RNA = adata_RNA[adata_RNA.obs.pct_counts_mt < 5, :].copy()

    # Ensure that gene IDs are unique in the RNA data
    adata_RNA.var.index = adata_RNA.var['gene_ids'].values
    adata_RNA.var_names_make_unique()
    adata_RNA.var['gene_ids'] = adata_RNA.var.index

    ### Aligning RNA and ATAC data by barcodes (cells)
    # Identify barcodes present in both RNA and ATAC data
    selected_barcode: list = list(set(adata_RNA.obs['barcode'].values) & set(adata_ATAC.obs['barcode'].values))

    # Filter RNA data to keep only the barcodes present in both RNA and ATAC datasets
    barcode_idx: pd.DataFrame = pd.DataFrame(range(adata_RNA.shape[0]), index=adata_RNA.obs['barcode'].values)
    adata_RNA = adata_RNA[barcode_idx.loc[selected_barcode][0]]

    # Filter ATAC data to keep only the barcodes present in both RNA and ATAC datasets
    barcode_idx = pd.DataFrame(range(adata_ATAC.shape[0]), index=adata_ATAC.obs['barcode'].values)
    adata_ATAC = adata_ATAC[barcode_idx.loc[selected_barcode][0]]

    # Return the filtered and annotated RNA and ATAC AnnData objects
    return adata_RNA, adata_ATAC

def combine_peaks_and_fragments(peak_bed_file, atac_fragments_file, tmp_dir):
    os.environ["TMPDIR"] = tmp_dir  # ensures large temp files go to scratch

    # Define output path for cached overlap file
    basename = os.path.splitext(os.path.basename(peak_bed_file))[0]
    overlap_cache_path = os.path.join(tmp_dir, f"{basename}_overlap_df.tsv")

    if os.path.exists(overlap_cache_path):
        logging.info(f"Found cached overlap file at {overlap_cache_path}, loading instead of recomputing")
        overlap_df = pd.read_csv(overlap_cache_path, sep="\t")
        return overlap_df

    # Read peaks.bed (3-column BED)
    peak_df = pd.read_csv(
        peak_bed_file,
        sep="\t",
        comment="#",
        header=None,
        names=["Chromosome", "Start", "End"]
    )

    # Read fragments.tsv
    fragments_df = pd.read_csv(
        atac_fragments_file,
        sep="\t",
        header=None,
        names=["Chromosome", "Start", "End", "Barcode", "Count"]
    )

    logging.info("Converting to PyRanges")
    peak_gr = pr.PyRanges(peak_df)
    frag_gr = pr.PyRanges(fragments_df)

    logging.info("Performing overlap join")
    overlap = peak_gr.join(frag_gr)
    logging.info(f"overlap.df.columns = {overlap.df.columns.tolist()}")

    # Rename and select columns
    overlap_df = overlap.df.rename(columns={
        "Chromosome": "peak_chr",
        "Start": "peak_start",
        "End": "peak_end",
        "Start_b": "frag_start",
        "End_b": "frag_end",
        "Barcode": "barcode",
        "Count": "count"
    })

    overlap_df = overlap_df[[
        "peak_chr", "peak_start", "peak_end",
        "frag_start", "frag_end",
        "barcode", "count"
    ]]

    # Save to disk for future use
    overlap_df.to_csv(overlap_cache_path, sep="\t", index=False)
    logging.info(f"Saved overlap_df to {overlap_cache_path}")

    return overlap_df


def convert_peak_frag_intersect_to_sparse_dataframe(
    overlap_df: pd.DataFrame,
    min_fragments_per_cell: int = 1000,
    min_peaks_per_cell: int = 100
) -> pd.DataFrame:
    logging.info(" - Filtering low-quality barcodes")

    # Calculate total fragments and number of peaks per barcode
    cell_stats = overlap_df.groupby("barcode").agg(
        total_fragments=("count", "sum"),
        nonzero_peaks=("peak_chr", "count")  # count of entries per barcode
    )

    # Keep barcodes with sufficient coverage
    high_quality_barcodes = cell_stats[
        (cell_stats["total_fragments"] >= min_fragments_per_cell) &
        (cell_stats["nonzero_peaks"] >= min_peaks_per_cell)
    ].index

    logging.info(f" - Keeping {len(high_quality_barcodes)} barcodes after QC filtering")

    # Filter overlap_df
    filtered_df = overlap_df[overlap_df["barcode"].isin(high_quality_barcodes)].copy()

    logging.info(" - Creating peak IDs")
    filtered_df["peak_id"] = (
        filtered_df["peak_chr"].astype(str) + ":" +
        filtered_df["peak_start"].astype(str) + "-" +
        filtered_df["peak_end"].astype(str)
    )

    # Pivot to dense matrix
    logging.info(" - Pivoting overlap DataFrame to peak × cell matrix")
    df_wide = filtered_df.pivot_table(
        index="peak_id",
        columns="barcode",
        values="count",
        aggfunc="sum",
        fill_value=0
    ).astype("float32")

    df_wide.reset_index(inplace=True)

    logging.info(f" - Final ATAC matrix shape: {df_wide.shape} (peaks x cells)")
    return df_wide

def convert_h5_gene_expression_to_count_matrix(h5_file_path):
    # Read 10X file
    adata = sc.read_10x_h5(h5_file_path)

    # Filter to RNA modality only
    if "feature_types" in adata.var.columns:
        adata = adata[:, adata.var["feature_types"] == "Gene Expression"]
        
    # Confirm shape
    logging.info(f"Shape: {adata.shape} (cells x genes)")
    
    # Transpose and convert to DataFrame
    rna_count_df = pd.DataFrame(
        data=adata.X.T.toarray(),  # gene × cell
        index=adata.var_names,     # gene names
        columns=adata.obs_names    # cell barcodes
    )
    
    rna_count_df["gene_id"] = rna_count_df.index
    rna_count_df.reset_index(drop=True, inplace=True)
    logging.info(rna_count_df.head())
    
    return rna_count_df

def convert_peak_fragment_h5_10X_files_to_count_matrices():
    data_dir = "/gpfs/Labs/Uzun/SCRIPTS/PROJECTS/2024.SINGLE_CELL_GRN_INFERENCE.MOELLER/input/mouse_kidney/10X_raw_data"
    peak_bed_file = os.path.join(data_dir, "M_Kidney_Chromium_Nuc_Isolation_vs_SaltyEZ_vs_ComplexTissueDP_atac_peaks.bed")
    atac_fragments_file = os.path.join(data_dir, "M_Kidney_Chromium_Nuc_Isolation_vs_SaltyEZ_vs_ComplexTissueDP_atac_fragments.tsv")
    h5_file_path = os.path.join(data_dir, "M_Kidney_Chromium_Nuc_Isolation_vs_SaltyEZ_vs_ComplexTissueDP_filtered_feature_bc_matrix.h5")
    tmp_dir = os.path.join(data_dir, "tmp_dir")
    output_dir = "/gpfs/Labs/Uzun/SCRIPTS/PROJECTS/2024.SINGLE_CELL_GRN_INFERENCE.MOELLER/input/mouse_kidney/mouse_kidney_sample1"

    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(tmp_dir, exist_ok=True)

    
    overlap_df = combine_peaks_and_fragments(peak_bed_file, atac_fragments_file, tmp_dir)
    atac_sparse_df = convert_peak_frag_intersect_to_sparse_dataframe(overlap_df)
    
    cols = ['peak_id'] + [col for col in atac_sparse_df.columns if col != 'peak_id']
    atac_sparse_df = atac_sparse_df[cols]

    logging.info(f'Exporting ATAC sparse matrix in long format')
    logging.info(atac_sparse_df.head())
    atac_output_file = os.path.join(output_dir, "mouse_kidney_ATAC.parquet")
    atac_sparse_df.to_parquet(atac_output_file, engine="pyarrow", compression="snappy", index=False)

    rna_count_df = convert_h5_gene_expression_to_count_matrix(h5_file_path)
    
    cols = ['gene_id'] + [col for col in rna_count_df.columns if col != 'gene_id']
    rna_count_df = rna_count_df[cols]

    logging.info(f'Exporting filtered ATAC data')
    rna_output_file = os.path.join(output_dir, "mouse_kidney_RNA.parquet")
    rna_count_df.to_parquet(rna_output_file, engine="pyarrow", compression="snappy", index=False)

def convert_matrix_features_barcodes_10X_files_to_count_matrices():
    base_dir = "/gpfs/Labs/Uzun/SCRIPTS/PROJECTS/2024.SINGLE_CELL_GRN_INFERENCE.MOELLER/input/DS011_mESC/10X_raw_data"
    # Read in the data files
    matrix=scipy.io.mmread(os.path.join(base_dir, "GSE198730_HIFLR_snRNA_matrix.mtx"))
    features=pd.read_csv(os.path.join(base_dir, "GSE198730_HIFLR_snRNA_features.tsv"),sep='\t',header=None)
    barcodes=pd.read_csv(os.path.join(base_dir, "GSE198730_HIFLR_snRNA_barcodes.tsv"),sep='\t',header=None)
    peaks=pd.read_csv(os.path.join(base_dir, "peaks.bed"), sep="\t")
    # ---------------------------------------------------

    logging.info('\nExtracting the adata RNA and ATAC seq data...')
    # Create AnnData objects for the scRNA-seq and scATAC-seq datasets
    adata_RNA, adata_ATAC = get_adata(matrix, features, barcodes)  # adata_RNA and adata_ATAC are scRNA and scATAC

    logging.info(f'\tscRNAseq Dataset: {adata_RNA.shape[0]} genes, {adata_RNA.shape[1]} cells')
    logging.info(f'\tscATACseq Dataset: {adata_ATAC.shape[0]} peaks, {adata_ATAC.shape[1]} cells')

    # Filter RNA data for a specific cell type
    # rna_filtered = adata_RNA[adata_RNA.obs['label'] == 'classical monocytes']

    # Extract the expression matrix (X) and convert it to a gene x cell DataFrame
    RNA_expression_matrix: pd.DataFrame = pd.DataFrame(
        data=adata_RNA.X.T.toarray(),  # Convert sparse matrix to dense
        index=adata_RNA.var['gene_ids'],    # Gene IDs as rows
        columns=adata_RNA.obs['barcode']  # Cell barcodes as columns
    )
    
    RNA_expression_matrix["gene_id"] = RNA_expression_matrix.index
    RNA_expression_matrix.reset_index(drop=True, inplace=True)
    
    # Make sure gene_id is column 0
    cols = ['gene_id'] + [col for col in RNA_expression_matrix.columns if col != 'gene_id']
    RNA_expression_matrix = RNA_expression_matrix[cols]
    logging.info(RNA_expression_matrix.head())

    output_dir = "/gpfs/Labs/Uzun/SCRIPTS/PROJECTS/2024.SINGLE_CELL_GRN_INFERENCE.MOELLER/input/DS011_mESC/DS011_mESC_sample1"

    # Export the filtered RNA expression matrix to a CSV file
    logging.info(f'\tExporting filtered RNA data')
    RNA_output_file = os.path.join(output_dir, "DS011_mESC_RNA.parquet")
    RNA_expression_matrix.to_parquet(RNA_output_file, engine="pyarrow", compression="snappy", index=False)

    # Filter ATAC data for 'classical monocytes'
    # atac_filtered = adata_ATAC[adata_ATAC.obs['label'] == 'classical monocytes']

    # Extract the expression matrix (X) and convert it to a gene x cell DataFrame
    ATAC_expression_matrix = pd.DataFrame(
        data=adata_ATAC.X.T.toarray(),
        index=adata_ATAC.var['gene_ids'],
        columns=adata_ATAC.obs['barcode']
    )
    ATAC_expression_matrix["peak_id"] = ATAC_expression_matrix.index
    ATAC_expression_matrix.reset_index(drop=True, inplace=True)
    
    # Make sure peak_id is column 0
    cols = ['peak_id'] + [col for col in ATAC_expression_matrix.columns if col != 'peak_id']
    ATAC_expression_matrix = ATAC_expression_matrix[cols]
    logging.info(ATAC_expression_matrix.head())

    # Export the filtered ATAC expression matrix to a CSV file
    logging.info(f'\tExporting filtered ATAC data')
    ATAC_output_file = os.path.join(output_dir, "DS011_mESC_ATAC.parquet")
    ATAC_expression_matrix.to_parquet(ATAC_output_file, engine="pyarrow", compression="snappy", index=False)
    
if __name__ == "__main__":
    # Configure logging
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    
    logging.info("\n----- Converting peak fragment h5 10X genomics project to count matrices -----")
    # convert_peak_fragment_h5_10X_files_to_count_matrices()
    
    logging.info("\n----- Converting matrix / features / barcodes 10X genomics project to count matrices -----")
    convert_matrix_features_barcodes_10X_files_to_count_matrices()