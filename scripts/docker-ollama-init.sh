#!/bin/sh
set -eu

create_model() {
    model_name="$1"
    model_directory="$2"
    model_definition="$3"
    model_file="/models/${model_directory}/${model_definition}"
    if [ ! -f "$model_file" ]; then
        echo "Missing model definition: $model_file" >&2
        exit 1
    fi
    ollama create "$model_name" -f "$model_file"
}

classifier_name="${OLLAMA_CLASSIFIER_MODEL:-xling-cls-3b-ft:latest}"
classifier_directory="${OLLAMA_CLASSIFIER_MODEL_DIR:-${classifier_name%%:*}}"
classifier_definition="${OLLAMA_CLASSIFIER_MODEL_FILE:-Modelfile}"
create_model "$classifier_name" "$classifier_directory" "$classifier_definition"

if [ "${AI_PROVIDER:-ollama}" = "ollama" ]; then
    answer_name="${OLLAMA_MODEL:-xling-qwen2.5-7b-ft:latest}"
    answer_directory="${OLLAMA_ANSWER_MODEL_DIR:-${answer_name%%:*}}"
    answer_definition="${OLLAMA_ANSWER_MODEL_FILE:-Modelfile}"
    create_model "$answer_name" "$answer_directory" "$answer_definition"
fi

echo "Ollama model registration completed."
