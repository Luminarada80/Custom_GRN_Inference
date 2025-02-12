#!/bin/bash -l

#SBATCH --job-name custom_grn_method
#SBATCH --partition compute
#SBATCH --nodes=1
#SBATCH --cpus-per-task 32
#SBATCH --mem-per-cpu=16G
#SBATCH --output=/dev/null
#SBATCH --error=/dev/null

set -euo pipefail

# =============================================
# SELECT WHICH PROCESSES TO RUN
# =============================================
CICERO_MAP_PEAKS_TO_TG=false
CREATE_HOMER_PEAK_FILE=false
HOMER_FIND_MOTIFS_GENOME=false
HOMER_PROCESS_MOTIF_FILES=false
PARSE_TF_PEAK_MOTIFS=false
CALCULATE_TF_REGULATION_SCORE=false
PROCESS_MOTIFS=true

# =============================================
# USER PATH VARIABLES
# =============================================
BASE_DIR=$(readlink -f \
    "/gpfs/Labs/Uzun/SCRIPTS/PROJECTS/2024.SINGLE_CELL_GRN_INFERENCE.MOELLER"
    )
INPUT_DIR="$BASE_DIR/input"
ATAC_DATA_FILE="$INPUT_DIR/multiomic_data_filtered_L2_E7.5_rep1_ATAC.csv"
RNA_DATA_FILE="$INPUT_DIR/multiomic_data_filtered_L2_E7.5_rep1_RNA.csv"
HOMER_ORGANISM_CODE="mm10" # Make sure you download the correct homer genome e.g. `perl Homer/configureHomer.pl -install mm10`
CONDA_ENV_NAME="my_env"

# Other paths
PYTHON_SCRIPT_DIR="$BASE_DIR/src/python_scripts"
R_SCRIPT_DIR="$BASE_DIR/src/r_scripts"
OUTPUT_DIR="$BASE_DIR/output"

CICERO_OUTPUT_FILE="$OUTPUT_DIR/peak_gene_associations.csv"

HOMER_DIR="$BASE_DIR/Homer/bin"
HOMER_PEAK_FILE="$INPUT_DIR/Homer_peaks.txt"
HOMER_KNOWN_MOTIF_DIR="$OUTPUT_DIR/homer_findMotifsGenome_output/knownResults"
HOMER_ANNOTATE_PEAKS_OUTPUT_DIR="$OUTPUT_DIR/homer_annotatePeaks_output"

TF_MOTIF_BINDING_SCORE_FILE="$OUTPUT_DIR/total_motif_regulatory_scores.tsv"

LOG_DIR="$BASE_DIR/LOGS"
FIG_DIR="$BASE_DIR/figures"

# Set output and error files dynamically
exec > "${LOG_DIR}/main_pipeline.log" 2> "${LOG_DIR}/main_pipeline.err"

# =============================================
# FUNCTIONS
# =============================================

# -------------- VALIDATION FUNCTIONS ----------------------

check_for_running_jobs() {
    echo "[INFO] Checking for running jobs with the same name..."
    if [ -z "${SLURM_JOB_NAME:-}" ]; then
        echo "    Not running in a SLURM environment, not checking for running tasks"
        return 0
    fi

    # Use the SLURM job name for comparison
    JOB_NAME="${SLURM_JOB_NAME:-custom_grn_inference}"  # Dynamically retrieve the job name from SLURM

    # Check for running jobs with the same name, excluding the current job
    RUNNING_COUNT=$(squeue --name="$JOB_NAME" --noheader | wc -l)

    # If other jobs with the same name are running, exit
    if [ "$RUNNING_COUNT" -gt 1 ]; then
        echo "[WARNING] A job with the name '"$JOB_NAME"' is already running:"
        echo "    Exiting to avoid conflicts."
        exit 1
    
    # If no other jobs are running, pass
    else
        echo "    No other jobs with the name '"$JOB_NAME"'"
    fi
}

validate_critical_variables() {
    # Make sure that all of the required user variables are set
    local critical_vars=(R_SCRIPT_DIR ATAC_DATA_FILE OUTPUT_DIR LOG_DIR HOMER_PEAK_FILE HOMER_ORGANISM_CODE)
    for var in "${critical_vars[@]}"; do
        if [ -z "${!var:-}" ]; then
            echo "[ERROR] Required variable $var is not set."
            exit 1
        fi
    done
}

validate_critical_variables

# Function to check if at least one process is selected
check_pipeline_steps() {
    if ! $CICERO_MAP_PEAKS_TO_TG && ! $CREATE_HOMER_PEAK_FILE && ! $HOMER_FIND_MOTIFS_GENOME && \
       ! $HOMER_PROCESS_MOTIF_FILES && ! $PARSE_TF_PEAK_MOTIFS && ! $CALCULATE_TF_REGULATION_SCORE \
       && ! $PROCESS_MOTIFS; then
        echo "Error: At least one process must be enabled to run the pipeline."
        exit 1
    fi
}

# Function to validate required tools
check_tools() {
    local required_tools=(perl python3 conda)

    echo "[INFO] Validating required tools."
    for tool in "${required_tools[@]}"; do
        if ! command -v $tool &> /dev/null; then
            echo "[ERROR] $tool is not installed or not in the PATH."
            exit 1
        else
            echo "[INFO] $tool is available."
        fi
    done

    # Handle GNU parallel
    if ! command -v parallel &> /dev/null; then
        echo "[INFO] GNU parallel not found in PATH. Attempting to load module..."
        if command -v module &> /dev/null; then
            module load parallel || {
                echo "[ERROR] Failed to load GNU parallel using module."
                exit 1
            }
            echo "[INFO] GNU parallel module loaded successfully."
        else
            echo "[ERROR] Module command not available. GNU parallel is required."
            exit 1
        fi
    else
        echo "[INFO] GNU parallel is available."
    fi
}

determine_num_cpus() {
    if [ -z "${SLURM_CPUS_PER_TASK:-}" ]; then
        if command -v nproc &> /dev/null; then
            TOTAL_CPUS=$(nproc --all)
            case $TOTAL_CPUS in
                [1-15]) IGNORED_CPUS=1 ;;  # Reserve 1 CPU for <=15 cores
                [16-31]) IGNORED_CPUS=2 ;; # Reserve 2 CPUs for <=31 cores
                *) IGNORED_CPUS=4 ;;       # Reserve 4 CPUs for >=32 cores
            esac
            NUM_CPU=$((TOTAL_CPUS - IGNORED_CPUS))
            echo "[INFO] Running locally. Detected $TOTAL_CPUS CPUs, reserving $IGNORED_CPUS for system tasks. Using $NUM_CPU CPUs."
        else
            NUM_CPU=1  # Fallback
            echo "[INFO] Running locally. Unable to detect CPUs, defaulting to $NUM_CPU CPU."
        fi
    else
        NUM_CPU=${SLURM_CPUS_PER_TASK}
        echo "[INFO] Running on SLURM. Number of CPUs allocated: ${NUM_CPU}"
    fi
}

install_homer() {
    # Set the Homer directory path
    HOMER_DIR="$BASE_DIR/Homer/bin"

    # Double-check if Homer directory already exists
    if [ -d "$BASE_DIR/Homer" ]; then
        echo "Homer directory found, adding to PATH"
        export PATH=$HOMER_DIR:$PATH
        return
    fi

    echo "    Creating Homer directory"
    mkdir -p "$BASE_DIR/Homer"

    echo "    Downloading Homer..."
    curl -s -o "$BASE_DIR/Homer/configureHomer.pl" http://homer.ucsd.edu/homer/configureHomer.pl
    if [ $? -ne 0 ]; then
        echo "[ERROR] Failed to download Homer."
        exit 1
    fi
    echo "        Done!"

    echo "    Installing Homer..."
    perl "$BASE_DIR/Homer/configureHomer.pl" -install
    if [ $? -ne 0 ]; then
        echo "[ERROR] Failed to install Homer."
        exit 1
    fi
    echo "        Done!"

    echo "    Downloading $HOMER_ORGANISM_CODE genome fasta"
    perl "$BASE_DIR/Homer/configureHomer.pl" -install "$HOMER_ORGANISM_CODE"
    if [ $? -ne 0 ]; then
        echo "[ERROR] Failed to download genome fasta for $HOMER_ORGANISM_CODE."
        exit 1
    fi
    echo "        Done!"

    # Export Homer bin directory to PATH
    export PATH=$HOMER_DIR:$PATH
    echo "    Added Homer to PATH: $HOMER_DIR"
}


# Function to validate input files
check_input_files() {
    echo "[INFO] Validating input files."
    local files=("$ATAC_DATA_FILE" "$RNA_DATA_FILE")
    for file in "${files[@]}"; do
        if [ ! -f "$file" ]; then
            echo "[ERROR] File not found: $file"
            exit 1
        elif [ ! -r "$file" ]; then
            echo "[ERROR] File is not readable: $file"
            exit 1
        fi
    done
    echo "[INFO] Input files validated successfully."
}

# Function to activate Conda environment
activate_conda_env() {
    CONDA_BASE=$(conda info --base)
    if [ -z "$CONDA_BASE" ]; then
        echo "Error: Conda base could not be determined. Is Conda installed and in your PATH?"
        exit 1
    fi

    source "$CONDA_BASE/bin/activate"
    if ! conda env list | grep -q "^$CONDA_ENV_NAME "; then
        echo "Error: Conda environment '$CONDA_ENV_NAME' does not exist."
        exit 1
    fi

    conda activate "$CONDA_ENV_NAME" || { echo "Error: Failed to activate Conda environment '$CONDA_ENV_NAME'."; exit 1; }
    echo "Activated Conda environment: $CONDA_ENV_NAME"
}

# Function to ensure required directories exist
setup_directories() {
    echo "[INFO] Ensuring required directories exist."
    local dirs=("$INPUT_DIR" "$OUTPUT_DIR" "$LOG_DIR" "$HOMER_ANNOTATE_PEAKS_OUTPUT_DIR")
    for dir in "${dirs[@]}"; do
        mkdir -p "$dir"
    done
    echo "    Required directories created."
}

check_r_environment() {
    REQUIRED_R_VERSION="4.3.2"  # Replace with your required version
    echo "    Checking R environment..."

    # Check if the 'module' command exists
    if ! command -v module &> /dev/null; then
        echo "        [ERROR] 'module' command is not available. Ensure the environment module system is installed."
        exit 1
    fi

    # Check if the 'rstudio' module is available
    if ! module avail rstudio &> /dev/null; then
        echo "        [ERROR] 'rstudio' module is not available. Check your module system."
        exit 1
    fi

    # Load the 'rstudio' module
    module load rstudio
    if [ $? -ne 0 ]; then
        echo "        [ERROR] Failed to load 'rstudio' module."
        exit 1
    else
        echo "        [INFO] Successfully loaded 'rstudio' module."
    fi

    # Check R version
    if ! command -v R &> /dev/null; then
        echo "        [ERROR] R is not installed. Please install R version $REQUIRED_R_VERSION or later."
        exit 1
    fi

    # Check if the installed version of R is different
    INSTALLED_R_VERSION=$(R --version | grep -oP "(?<=R version )\d+\.\d+\.\d+" | head -1)
    if [[ "$(printf '%s\n' "$REQUIRED_R_VERSION" "$INSTALLED_R_VERSION" | sort -V | head -1)" != "$REQUIRED_R_VERSION" ]]; then
        echo "        [ERROR] Installed R version ($INSTALLED_R_VERSION) is older than required ($REQUIRED_R_VERSION). Please update R."
        exit 1
    fi
    echo "        R version $INSTALLED_R_VERSION is installed."

    # Check for required R packages
    Rscript $R_SCRIPT_DIR/check_dependencies.r
    echo ""
}

download_file_if_missing() {
    local file_path=$1
    local file_url=$2
    local file_description=$3

    if [ ! -f "$file_path" ]; then
        echo "    $file_description not found, downloading..."
        curl -s -o "$file_path" "$file_url"

        if [ $? -ne 0 ] || [ ! -s "$file_path" ]; then
            echo "[ERROR] Failed to download or validate $file_description from $file_url."
            exit 1
        else
            echo "        Successfully downloaded $file_description"
        fi
    else
        echo "        Using existing $file_description"
    fi
}

check_cicero_genome_files_exist() {

    if [ "$HOMER_ORGANISM_CODE" == "mm10" ]; then
        echo "    $HOMER_ORGANISM_CODE detected, using mouse genome"

        CHROM_SIZES="$INPUT_DIR/mm10.chrom.sizes"
        CHROM_SIZES_URL="https://hgdownload.soe.ucsc.edu/goldenPath/mm10/bigZips/mm10.chrom.sizes"

        GENE_ANNOT="$INPUT_DIR/Mus_musculus.GRCm39.113.gtf.gz"
        GENE_ANNOT_URL="https://ftp.ensembl.org/pub/release-113/gtf/mus_musculus/Mus_musculus.GRCm39.113.gtf.gz"
    
    elif [ "$HOMER_ORGANISM_CODE" == "hg38" ]; then
        echo "    $HOMER_ORGANISM_CODE detected, using human genome"

        CHROM_SIZES="$INPUT_DIR/hg38.chrom.sizes"
        CHROM_SIZES_URL="https://hgdownload.soe.ucsc.edu/goldenPath/hg38/bigZips/hg38.chrom.sizes"

        GENE_ANNOT="$INPUT_DIR/Homo_sapiens.GRCh38.113.gtf.gz"
        GENE_ANNOT_URL="https://ftp.ensembl.org/pub/release-113/gtf/homo_sapiens/Homo_sapiens.GRCh38.113.gtf.gz"

    else
        echo "    [ERROR] Unsupported HOMER_ORGANISM_CODE: $HOMER_ORGANISM_CODE"
        exit 1
    fi

    # Check chromosome sizes and gene annotation files
    download_file_if_missing "$CHROM_SIZES" "$CHROM_SIZES_URL" "$HOMER_ORGANISM_CODE chromosome sizes file"
    download_file_if_missing "$GENE_ANNOT" "$GENE_ANNOT_URL" "$HOMER_ORGANISM_CODE gene annotation file"
}

# -------------- MAIN PIPELINE FUNCTIONS --------------
run_cicero() {
    echo ""
    echo "Cicero: Mapping scATACseq peaks to target genes"

    # Validate variables
    if [[ -z "$R_SCRIPT_DIR" || -z "$ATAC_DATA_FILE" || -z "$OUTPUT_DIR" || -z "$LOG_DIR" ]]; then
        echo "[ERROR] One or more required variables (R_SCRIPT_DIR, ATAC_DATA_FILE, OUTPUT_DIR, LOG_DIR) are not set."
        exit 1
    fi

    # Ensure log directory exists
    mkdir -p "$LOG_DIR"

    # Check R environment
    check_r_environment

    # Check for the chomosome size and gene annotation files in INPUT_DIR
    check_cicero_genome_files_exist

    # Load R module (optional, for HPC systems)
    if command -v module &> /dev/null; then
        module load rstudio
    fi

    echo "    Checks complete, running Cicero"

    /usr/bin/time -v \
    Rscript "$R_SCRIPT_DIR/cicero.r" \
        "$ATAC_DATA_FILE" \
        "$OUTPUT_DIR" \
        "$CHROM_SIZES" \
        "$GENE_ANNOT" \
    > "$LOG_DIR/step01_run_cicero.log" 2>"$LOG_DIR/cicero_R_output.log"
}

create_homer_peak_file() {
    echo ""
    echo "Python: Creating Homer peak file"
    /usr/bin/time -v \
    python3 "$PYTHON_SCRIPT_DIR/Step010.create_homer_peak_file.py" \
        --atac_data_file "$ATAC_DATA_FILE" \
        --homer_peak_file "$HOMER_PEAK_FILE" \
    > "$LOG_DIR/step02_create_homer_peaks.log"
}

find_motifs_genome() {
    echo ""
    echo "Homer: Running findMotifsGenome.pl"

    mkdir -p "$OUTPUT_DIR/homer_findMotifsGenome_output"

    /usr/bin/time -v \
    perl "$HOMER_DIR/findMotifsGenome.pl" "$HOMER_PEAK_FILE" "$HOMER_ORGANISM_CODE" "$OUTPUT_DIR/homer_findMotifsGenome_output" -size 200 \
    2> "$LOG_DIR/step03_homer_findMotifsGenome.log"
}

homer_process_motif_files() {
    echo "[INFO] Starting motif file processing"

    # Check for GNU parallel
    if ! command -v parallel &> /dev/null; then
        echo "[INFO] GNU parallel not found. Falling back to sequential processing."
        use_parallel=false
    else
        use_parallel=true
        echo "[INFO] GNU parallel detected."
    fi

    # Detect files to process
    motif_files=$(find "$HOMER_KNOWN_MOTIF_DIR" -name "*.motif")
    if [ -z "$motif_files" ]; then
        echo "[ERROR] No motif files found in $HOMER_KNOWN_MOTIF_DIR."
        exit 1
    fi

    # Log number of files to process
    file_count=$(echo "$motif_files" | wc -l)
    echo "[INFO] Found $file_count motif files to process."

    # Create output directory if it doesn't exist
    mkdir -p "$HOMER_ANNOTATE_PEAKS_OUTPUT_DIR"

    # Process files in parallel
    if [ "$use_parallel" = true ]; then
        echo "$motif_files" | /usr/bin/time -v parallel -j "$NUM_CPU" \
            "annotatePeaks.pl $HOMER_PEAK_FILE '$HOMER_ORGANISM_CODE' -m {} > $HOMER_ANNOTATE_PEAKS_OUTPUT_DIR/{/}_tf_motifs.txt" \
            >> "$LOG_DIR/step05_parallel.log" 2>>"$LOG_DIR/step05_parallel.err"
    
    # Process files sequentially
    else
        for file in $motif_files; do
            local output_file="$HOMER_ANNOTATE_PEAKS_OUTPUT_DIR/$(basename "$file" .motif)_tf_motifs.txt"
            /usr/bin/time -v \
            perl "$HOMER_DIR/annotatePeaks.pl" "$HOMER_PEAK_FILE" "$HOMER_ORGANISM_CODE" -m "$file" > "$output_file" \
            2>> "$LOG_DIR/step05_sequential.err"

            if [ $? -ne 0 ]; then
                echo "[ERROR] Failed to process motif file: $file" >> "$LOG_DIR/step05_sequential.err"
            else
                echo "[INFO] Successfully processed: $file"
            fi
        done
    fi

    # Check for errors
    if [ $? -ne 0 ]; then
        echo "[ERROR] Motif file processing failed. Check logs in $LOG_DIR for details."
        exit 1
    fi

    echo "[INFO] Motif file processing completed successfully."
}

parse_tf_peak_motifs() {
    echo ""
    echo "Python: Parsing TF binding motif results from Homer"
    /usr/bin/time -v \
    python3 "$PYTHON_SCRIPT_DIR/Step060.parse_TF_peak_motifs.py" \
        --input_dir "$HOMER_ANNOTATE_PEAKS_OUTPUT_DIR" \
        --cicero_cis_reg_file "$CICERO_OUTPUT_FILE" \
        --homer_peak_file "$HOMER_PEAK_FILE" \
        --output_file "$TF_MOTIF_BINDING_SCORE_FILE" \
        --cpu_count "$NUM_CPU" \
    > "$LOG_DIR/step06_parse_tf_binding_motifs.log"
}

calculate_tf_regulation_score() {
    echo ""
    echo "Python: Calculating TF-TG regulatory potential"
    /usr/bin/time -v \
    python3 "$PYTHON_SCRIPT_DIR/Step070.find_overlapping_TFs.py" \
        --rna_data_file "$RNA_DATA_FILE" \
        --tf_motif_binding_score_file "$TF_MOTIF_BINDING_SCORE_FILE" \
        --output_dir "$OUTPUT_DIR" \
        --fig_dir "$FIG_DIR" \
        > "$LOG_DIR/step07_calculate_tf_tg_regulatory_potential.log"
}

process_motifs() {
    echo ""
    echo "Python: Processing Motifs"
    /usr/bin/time -v \
    python3 "/gpfs/Labs/Uzun/SCRIPTS/PROJECTS/2024.SINGLE_CELL_GRN_INFERENCE.MOELLER/yasin_motif_binding_code/sliding_window_tf_binding.py" \
    > "$LOG_DIR/sliding_window_tf_binding.log"
}

# =============================================
# MAIN PIPELINE
# =============================================

# Help option
if [[ "${1:-}" == "--help" ]]; then
    echo "Usage: bash main_pipeline.sh"
    echo "This script executes a single-cell GRN inference pipeline."
    echo "Modify the flags at the top of the script to enable/disable steps."
    exit 0
fi

# Perform validation
check_for_running_jobs
check_pipeline_steps
check_tools
determine_num_cpus
check_input_files
activate_conda_env
setup_directories
install_homer

# Execute selected pipeline steps
if [ "$CICERO_MAP_PEAKS_TO_TG" = true ]; then run_cicero; fi
if [ "$CREATE_HOMER_PEAK_FILE" = true ]; then create_homer_peak_file; fi
if [ "$HOMER_FIND_MOTIFS_GENOME" = true ]; then find_motifs_genome; fi
if [ "$HOMER_PROCESS_MOTIF_FILES" = true ]; then homer_process_motif_files; fi
if [ "$PARSE_TF_PEAK_MOTIFS" = true ]; then parse_tf_peak_motifs; fi
if [ "$CALCULATE_TF_REGULATION_SCORE" = true ]; then calculate_tf_regulation_score; fi
if [ "$PROCESS_MOTIFS" = true ]; then process_motifs; fi
