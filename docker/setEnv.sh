#!/bin/bash

# docker-compose.*.yml files names and their position compared to this script.
# Here: in parent directory.
target_dir="${0%/*}/.."
override_link='docker-compose.override.yml'
override_file_tests='docker-compose.tests.yml'
override_file_integration='docker-compose.integration.yml'


# Get the current environment and tells what are the options
function show_current {
    get_current
    say_switch
}


# Get the current environment
# Output variable: current_env
function get_current {
    if [ -L ${override_link} ]
    then
        # Check for Mac OSX
        if [[ "$OSTYPE" == "darwin"* ]]; then
            # readlink is not native to mac, so this will work in it's place.
            symlink=$(python3 -c "import os; print(os.path.realpath('docker-compose.override.yml'))")
        else
            # Maintain the cleaner way
            symlink=$(readlink -f docker-compose.override.yml)
        fi
        basename_symlink=$(basename "$symlink")
        if [ "$basename_symlink" = "$override_file_tests" ]; then
            current_env=tests
        elif [ "$basename_symlink" = "$override_file_integration" ]; then
            current_env=integration
        else
            current_env=unknown
        fi
    else
        current_env=release
    fi
}

# Tell to which environments we can switch
function say_switch {
    echo "Using '${current_env}' configuration."
    for one_env in tests integration release
    do
        if [ "${current_env}" != ${one_env} ]; then
            echo "-> You can switch to '${one_env}' with '${0} ${one_env}'"
        fi
    done
}


function set_release {
    get_current
    if [ "${current_env}" != release ]
    then
        docker compose down
        # In release configuration there is no override file
        rm ${override_link}
        echo "Now using 'release' configuration."
    else
        echo "Already using 'release' configuration."
    fi
}


function set_tests {
    get_current
    if [ "${current_env}" != tests ]
    then
        docker compose down
        rm -f ${override_link}
        ln -s ${override_file_tests} ${override_link}
        echo "Now using 'tests' configuration."
    else
        echo "Already using 'tests' configuration."
    fi
}

function set_integration {
    get_current
    if [ "${current_env}" != integration ]
    then
        docker compose down
        rm -f ${override_link}
        ln -s ${override_file_integration} ${override_link}
        echo "Now using 'integration' configuration."
    else
        echo "Already using 'integration' configuration."
    fi
}

# Change directory to allow working with relative paths.
cd "${target_dir}" || exit

if [ ${#} -eq 1 ] && [[ 'tests integration release' =~ ${1} ]]
then
    set_"${1}"
else
    show_current
fi
