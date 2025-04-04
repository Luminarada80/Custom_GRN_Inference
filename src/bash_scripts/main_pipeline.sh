#!/bin/bash -l

#SBATCH --partition compute
#SBATCH --nodes=1
#SBATCH --cpus-per-task 10
#SBATCH --mem 64G

set -euo pipefail

# =============================================
# SELECT WHICH PROCESSES TO RUN
# =============================================
STEP010_CICERO_MAP_PEAKS_TO_TG=false
STEP020_CICERO_PEAK_TO_TG_SCORE=false
STEP025_PEAK_TO_TG_CORRELATION=false
STEP030_SLIDING_WINDOW_TF_TO_PEAK_SCORE=false
STEP035_HOMER_TF_TO_PEAK_SCORE=false
STEP040_TF_TO_TG_SCORE=false
STEP050_TRAIN_CLASSIFIER=true

# =============================================
# USER PATH VARIABLES
# =============================================
CONDA_ENV_NAME="my_env"

BASE_DIR=$(readlink -f "/gpfs/Labs/Uzun/SCRIPTS/PROJECTS/2024.SINGLE_CELL_GRN_INFERENCE.MOELLER")

# Input file paths
INPUT_DIR="${BASE_DIR}/input/${CELL_TYPE}/${SAMPLE_NAME}"
RNA_FILE_NAME="$INPUT_DIR/$RNA_FILE_NAME"
ATAC_FILE_NAME="$INPUT_DIR/$ATAC_FILE_NAME"

# Other paths
PYTHON_SCRIPT_DIR="$BASE_DIR/src/python_scripts"
R_SCRIPT_DIR="$BASE_DIR/src/r_scripts"
OUTPUT_DIR="$BASE_DIR/output/$CELL_TYPE/$SAMPLE_NAME"
REFERENCE_GENOME_DIR="$BASE_DIR/reference_genome/$SPECIES"
ENHANCERDB_FILE="$BASE_DIR/enhancer_db/enhancer"
INFERRED_NET_FILE="$OUTPUT_DIR/inferred_network_raw.csv"

TF_NAMES_FILE="$BASE_DIR/motif_information/$SPECIES/TF_Information_all_motifs.txt"
MEME_DIR="$BASE_DIR/motif_information/$SPECIES/${SPECIES}_motif_meme_files"

# LOG_DIR="$BASE_DIR/LOGS/${SAMPLE_NAME}/"
FIG_DIR="$BASE_DIR/figures/$SPECIES/$SAMPLE_NAME"

# mkdir -p "${LOG_DIR}"
mkdir -p "${FIG_DIR}"
LOG_DIR="${BASE_DIR}/LOGS/${CELL_TYPE}_logs/${SAMPLE_NAME}_logs/"

if [ "$SPECIES" == "human" ]; then
    SPECIES="hg38"
elif [ "$SPECIES" == "mouse" ]; then
    SPECIES="mm10"
fi

echo "Input files:"
echo "    RNA Data File: $RNA_FILE_NAME"
echo "    ATAC Data File: $ATAC_FILE_NAME"
echo "    Cell Type: $CELL_TYPE"
echo "    Sample: $SAMPLE_NAME"
echo "    Species: $SPECIES"
echo ""

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
    JOB_NAME="${SLURM_JOB_NAME:-custom_grn_method}"  # Dynamically retrieve the job name from SLURM

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
    local critical_vars=(
        SAMPLE_NAME \
        SPECIES \
        BASE_DIR \
        RNA_FILE_NAME \
        ATAC_FILE_NAME \
        OUTPUT_DIR \
        LOG_DIR \
        TF_NAMES_FILE \
        CONDA_ENV_NAME
        )
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
    if ! $STEP010_CICERO_MAP_PEAKS_TO_TG \
    && ! $STEP020_CICERO_PEAK_TO_TG_SCORE \
    && ! $STEP025_PEAK_TO_TG_CORRELATION \
    && ! $STEP030_SLIDING_WINDOW_TF_TO_PEAK_SCORE \
    && ! $STEP035_HOMER_TF_TO_PEAK_SCORE \
    && ! $STEP040_TF_TO_TG_SCORE \
    && ! $STEP050_TRAIN_CLASSIFIER; then \
        echo "Error: At least one process must be enabled to run the pipeline."
        exit 1
    fi
}

# Function to validate required tools
check_tools() {
    local required_tools=(python3 conda)

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

# Function to validate input files
check_input_files() {
    echo "[INFO] Validating input files."
    local files=("$ATAC_FILE_NAME" "$RNA_FILE_NAME")
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
    local dirs=( 
        "$INPUT_DIR" \
        "$OUTPUT_DIR" \
        "$LOG_DIR" \
        "$FIG_DIR" \
        )

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

    if [ "$SPECIES" == "mm10" ]; then
        echo "    $SPECIES detected, using mouse genome"

        CHROM_SIZES="$REFERENCE_GENOME_DIR/mm10.chrom.sizes"
        CHROM_SIZES_URL="https://hgdownload.soe.ucsc.edu/goldenPath/mm10/bigZips/mm10.chrom.sizes"

        GENE_ANNOT="$REFERENCE_GENOME_DIR/Mus_musculus.GRCm39.113.gtf.gz"
        GENE_ANNOT_URL="https://ftp.ensembl.org/pub/release-113/gtf/mus_musculus/Mus_musculus.GRCm39.113.gtf.gz"
    
    elif [ "$SPECIES" == "hg38" ]; then
        echo "    $SPECIES detected, using human genome"

        CHROM_SIZES="$REFERENCE_GENOME_DIR/hg38.chrom.sizes"
        CHROM_SIZES_URL="https://hgdownload.soe.ucsc.edu/goldenPath/hg38/bigZips/hg38.chrom.sizes"

        GENE_ANNOT="$REFERENCE_GENOME_DIR/Homo_sapiens.GRCh38.113.gtf.gz"
        GENE_ANNOT_URL="https://ftp.ensembl.org/pub/release-113/gtf/homo_sapiens/Homo_sapiens.GRCh38.113.gtf.gz"

    else
        echo "    [ERROR] Unsupported SPECIES: $SPECIES"
        exit 1
    fi

    # Check chromosome sizes and gene annotation files
    download_file_if_missing "$CHROM_SIZES" "$CHROM_SIZES_URL" "$SPECIES chromosome sizes file"
    download_file_if_missing "$GENE_ANNOT" "$GENE_ANNOT_URL" "$SPECIES gene annotation file"
}

run_dataset_preprocessing() {
    echo ""
    echo "Python: Log2 CPM normalizing the ATAC-seq and RNA-seq datasets"
    /usr/bin/time -v \
    python3 "$PYTHON_SCRIPT_DIR/preprocess_datasets.py" \
        --atac_data_file "$ATAC_FILE_NAME" \
        --rna_data_file "$RNA_FILE_NAME"
    
    # After preprocessing, update the file names to the new processed files.
    # This assumes that the processed file name is constructed by replacing
    # the original file extension with _processed.tsv.
    new_atac_file="$(dirname "$ATAC_FILE_NAME")/$(basename "$ATAC_FILE_NAME" | sed 's/\.[^.]*$/_processed.csv/')"
    new_rna_file="$(dirname "$RNA_FILE_NAME")/$(basename "$RNA_FILE_NAME" | sed 's/\.[^.]*$/_processed.csv/')"
    
    ATAC_FILE_NAME="$new_atac_file"
    RNA_FILE_NAME="$new_rna_file"
    
} 2> "$LOG_DIR/dataset_preprocessing.log"

check_processed_files() {
    # Derive the expected processed filenames from the original file names.
    new_atac_file="$(dirname "$ATAC_FILE_NAME")/$(basename "$ATAC_FILE_NAME" | sed 's/\.[^.]*$/_processed.csv/')"
    new_rna_file="$(dirname "$RNA_FILE_NAME")/$(basename "$RNA_FILE_NAME" | sed 's/\.[^.]*$/_processed.csv/')"
    
    if [ -f "$new_atac_file" ] && [ -f "$new_rna_file" ]; then
        echo "Processed files found:"
        echo "  ATAC: $new_atac_file"
        echo "  RNA:  $new_rna_file"
        # Update global variables so downstream steps use the processed files
        ATAC_FILE_NAME="$new_atac_file"
        RNA_FILE_NAME="$new_rna_file"
    else
        echo "Processed files not found. Running dataset preprocessing..."
        run_dataset_preprocessing
    fi
}

# -------------- HOMER FUNCTIONS --------------
install_homer() {
    echo "Installing Homer..."
    mkdir -p "$BASE_DIR/homer"
    wget "http://homer.ucsd.edu/homer/configureHomer.pl" -P "$BASE_DIR/homer"
    perl "$BASE_DIR/homer/configureHomer.pl" -install
    echo "    Done!"
} 2> "$LOG_DIR/Homer_logs/01.install_homer.log"

install_homer_species_genome() {
    echo "Installing Homer $SPECIES genome..."
    perl "$BASE_DIR/homer/configureHomer.pl" -install "$SPECIES"
    echo "    Done!"
} 2> "$LOG_DIR/Homer_logs/02.install_homer_species.log"

create_homer_peak_file() {
    echo "Creating Homer motif file from ATAC-seq peaks"
    /usr/bin/time -v \
    python3 "$PYTHON_SCRIPT_DIR/create_homer_peak_file.py" \
        --atac_data_file "$ATAC_FILE_NAME" \
        --output_dir "$OUTPUT_DIR"
    echo "    Done!"
} 2> "$LOG_DIR/Homer_logs/03.create_homer_peak_file.log"

homer_find_motifs() {
    echo "Running Homer findMotifsGenome"
    mkdir -p "$OUTPUT_DIR/homer_results"
    perl "$BASE_DIR/homer/bin/findMotifsGenome.pl" "$OUTPUT_DIR/homer_peaks.txt" "$SPECIES" "$OUTPUT_DIR/homer_results/" -size 200
    echo "    Done!"
} 2> "$LOG_DIR/Homer_logs/04.homer_findMotifsGenome.log"

homer_process_motif_files() {
    echo ""
    echo "----- Homer annotatePeaks.pl -----"
    echo "[INFO] Starting motif file processing"

    # Check for GNU parallel
    if ! command -v parallel &> /dev/null; then
        echo "[INFO] GNU parallel not found. Falling back to sequential processing."
        use_parallel=false
    else
        use_parallel=true
        echo "[INFO] GNU parallel detected."
    fi

    # Look through the knownResults dir of the Homer findMotifsGenome.pl output
    MOTIF_DIR="$OUTPUT_DIR/homer_results/knownResults"
    # Detect files to process
    motif_files=$(find "$MOTIF_DIR" -name "*.motif")
    if [ -z "$motif_files" ]; then
        echo "[ERROR] No motif files found in $MOTIF_DIR."
        exit 1
    fi

    # Log number of files to process
    file_count=$(echo "$motif_files" | wc -l)
    echo "[INFO] Found $file_count motif files to process."

    
    # Create output directory if it doesn't exist
    PROCESSED_MOTIF_DIR="$OUTPUT_DIR/homer_results/homer_tf_motif_scores"
    mkdir -p "$PROCESSED_MOTIF_DIR"

    # Process files in parallel
    if [ "$use_parallel" = true ]; then
        echo "$motif_files" | /usr/bin/time -v parallel -j "$NUM_CPU" \
            "perl $BASE_DIR/homer/bin/annotatePeaks.pl $OUTPUT_DIR/homer_peaks.txt '$SPECIES' -m {} > $PROCESSED_MOTIF_DIR/{/}_tf_motifs.txt"
    
    # Process files sequentially
    else
        for file in $motif_files; do
            local output_file="$PROCESSED_MOTIF_DIR/$(basename "$file" .motif)_tf_motifs.txt"
            /usr/bin/time -v \
            "perl $BASE_DIR/homer/bin/annotatePeaks.pl $OUTPUT_DIR/homer_peaks.txt '$SPECIES' -m $file > $output_file" \

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
    echo ""
} 2> "$LOG_DIR/Homer_logs/05.homer_annotatePeaks.log"

# -------------- MAIN PIPELINE FUNCTIONS --------------
run_cicero() {
    echo ""
    echo "Cicero: Mapping scATACseq peaks to target genes"

    # Validate variables
    if [[ -z "$R_SCRIPT_DIR" || -z "$ATAC_FILE_NAME" || -z "$OUTPUT_DIR" || -z "$LOG_DIR" ]]; then
        echo "[ERROR] One or more required variables (R_SCRIPT_DIR, ATAC_FILE_NAME, OUTPUT_DIR, LOG_DIR) are not set."
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
    Rscript "$R_SCRIPT_DIR/Step010.run_cicero.r" \
        "$ATAC_FILE_NAME" \
        "$OUTPUT_DIR" \
        "$CHROM_SIZES" \
        "$GENE_ANNOT" 
    
    # Unload the rstudio module and re-activate the conda environment after running the first step
    module unload rstudio
    activate_conda_env

} 2> "$LOG_DIR/Step010.run_cicero.log"

run_cicero_peak_to_tg_score() {
    echo ""
    echo "Python: Parsing Cicero peak to TG scores"
    /usr/bin/time -v \
    python3 "$PYTHON_SCRIPT_DIR/Step020.cicero_peak_to_tg_score.py" \
        --fig_dir "$FIG_DIR" \
        --output_dir "$OUTPUT_DIR" 
    
} 2> "$LOG_DIR/Step020.cicero_peak_to_tg_score.log"

run_correlation_peak_to_tg_score() {
    if [ ! -f $ENHANCERDB_FILE ]; then
        echo "EnhancerDB file not found, downloading..."
        mkdir -p "$BASE_DIR/enhancer_db"
        wget "https://lcbb.swjtu.edu.cn/EnhancerDB/_download/enhancer.gz" -P "$BASE_DIR/enhancer_db/"
        gunzip "$BASE_DIR/enhancer_db/enhancer.gz"
    fi

    echo ""
    echo "Python: Calculating correlation peak to TG score"
    /usr/bin/time -v \
    python3 "$PYTHON_SCRIPT_DIR/Step025.peak_gene_correlation.py" \
        --atac_data_file "$ATAC_FILE_NAME" \
        --rna_data_file "$RNA_FILE_NAME" \
        --tmp_dir "$OUTPUT_DIR/tmp" \
        --output_dir "$OUTPUT_DIR" \
        --species "$SPECIES" \
        --num_cpu "$NUM_CPU" \
        --peak_dist_limit 1000000

} 2> "$LOG_DIR/Step025.peak_to_gene_correlation.log"

run_homer() {
    echo ""
    echo "===== Homer ====="
    mkdir -p "$LOG_DIR/Homer_logs/"

    # Check to make sure Homer is installed, else install it
    if [ ! -d "$BASE_DIR/homer" ]; then
        echo ""
        echo "Homer installation not found"
        install_homer
    else
        echo "Found installation of homer"
    fi

    # Make sure that the homer directory is part of the path
    echo "Adding the 'homer/bin' directory to PATH"
    PATH="$PATH:$BASE_DIR/homer/bin"
    export PATH
    
    # Check if the species Homer genome is installed
    if perl "$BASE_DIR/homer/configureHomer.pl" -list | grep -q "^+.*${SPECIES}"; then
        echo "${SPECIES} genome is installed"
    else
        echo "${SPECIES} genome is not installed"
        install_homer_species_genome
    fi

    if [ ! -f "$OUTPUT_DIR/homer_peaks.txt" ]; then
        echo "Homer peak file not found"
        create_homer_peak_file
    else
        echo "Homer peak file found"
    fi

    # If the homer_results directory doesn't exist for the sample, run findMotifsGenome
    if [ ! -d "$OUTPUT_DIR/homer_results/" ]; then
        echo "Running Homer findMotifsGenome to identify enriched motifs in the ATAC-seq peaks"
        homer_find_motifs
    else
        echo "Found existing Homer findMotifsGenome results"
    fi

    if [ ! -d "$OUTPUT_DIR/homer_results/homer_tf_motif_scores/" ]; then
        echo "Running Homer annotatePeaks to find instances of specific motifs in the ATAC-seq peaks"
        homer_process_motif_files
    else
        echo "Found existing Homer annotatePeaks results"
    fi
    echo "Finished running Homer"
}

run_homer_tf_to_peak_score() {
    echo ""
    echo "Python: Calculating homer TF to peak scores"
    /usr/bin/time -v \
    python3 src/python_scripts/Step030.homer_tf_peak_motifs.py \
        --input_dir "${OUTPUT_DIR}/homer_results/homer_tf_motif_scores" \
        --output_file "${OUTPUT_DIR}/homer_tf_to_peak.tsv" \
        --cpu_count $NUM_CPU

} 2> "$LOG_DIR/Step030.homer_tf_to_peak_motifs.log"

run_sliding_window_tf_to_peak_score() {
    echo ""
    echo "Python: Calculating sliding window TF to peak scores"
    /usr/bin/time -v \
    python3 "$PYTHON_SCRIPT_DIR/Step035.sliding_window_tf_peak_motifs.py" \
        --tf_names_file "$TF_NAMES_FILE"\
        --meme_dir "$MEME_DIR"\
        --reference_genome_dir "$REFERENCE_GENOME_DIR"\
        --atac_data_file "$ATAC_FILE_NAME" \
        --rna_data_file "$RNA_FILE_NAME" \
        --output_dir "$OUTPUT_DIR" \
        --species "$SPECIES" \
        --num_cpu "$NUM_CPU" 
    
} 2> "$LOG_DIR/Step035.sliding_window_tf_peak_motifs.log"

run_tf_to_tg_score() {
    echo ""
    echo "Python: Creating TF to TG score DataFrame"
    /usr/bin/time -v \
    python3 "$PYTHON_SCRIPT_DIR/Step040.combine_dataframes.py" \
        --rna_data_file "$RNA_FILE_NAME" \
        --atac_data_file "$ATAC_FILE_NAME" \
        --output_dir "$OUTPUT_DIR" \
        --fig_dir "$FIG_DIR"
    
} 2> "$LOG_DIR/Step040.tf_to_tg_score.log"

run_classifier_training() {
    echo ""
    echo "Python: Training XGBoost Classifier"
    /usr/bin/time -v \
    python3 "$PYTHON_SCRIPT_DIR/Step050.train_xgboost.py" \
        --ground_truth_file "$GROUND_TRUTH_FILE" \
        --inferred_network_file "$INFERRED_NET_FILE" \
        --output_dir "$OUTPUT_DIR" \
        --fig_dir "$FIG_DIR" 

} 2> "$LOG_DIR/Step050.train_classifier.log"


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
check_processed_files

# Execute selected pipeline steps
if [ "$STEP010_CICERO_MAP_PEAKS_TO_TG" = true ]; then run_cicero; fi
if [ "$STEP020_CICERO_PEAK_TO_TG_SCORE" = true ]; then run_cicero_peak_to_tg_score; fi
if [ "$STEP025_PEAK_TO_TG_CORRELATION" = true ]; then run_correlation_peak_to_tg_score; fi
if [ "$STEP030_SLIDING_WINDOW_TF_TO_PEAK_SCORE" = true ]; then run_sliding_window_tf_to_peak_score; fi
if [ "$STEP035_HOMER_TF_TO_PEAK_SCORE" = true ]; then run_homer; run_homer_tf_to_peak_score; fi
if [ "$STEP040_TF_TO_TG_SCORE" = true ]; then run_tf_to_tg_score; fi
if [ "$STEP050_TRAIN_CLASSIFIER" = true ]; then run_classifier_training; fi