#!/bin/bash

# Tool for purging ECR repositories
# NOTE: ecr-purger's goal is to delete any image that isn't needed anymore from the repo; our build system continuously 
# pushes images (used for both QA and Prod deployments) that can sit undeployed in the repo for some time i.e. during 
# QA/testing or before a production deployment is triggered; so for this reason we need to make sure to only delete images
# that are 1- not in any active taskdef (allowing us to rollback) and 2- are older than let's say the one deployed in the
# (latest taskdef - n revisions), giving us some "wiggle_room" to account for images that are still being tested in QA or
# are queued up for deployment.

if [ "${1}" == "start" ]
then
    cd /opt/ecr-purger/
    python /opt/ecr-purger/ecr-purger.py
else
	exec $@
fi