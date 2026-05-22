#!/bin/bash

tenant=cortex-cx
tenant=cortex-local

cortex -t ${tenant} catalog delete-by-type --types team

cortex -t ${tenant} integrations workday delete

cortex -t ${tenant} integrations workday add --file cortex-team-list/configuration.json
