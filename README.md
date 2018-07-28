# ecr-purger
Tool for purging ECR repositories. NOTE: ecr-purger's goal is to delete any image that isn't needed anymore from the repo; it attempts to only delete images
that are 1- not in any active taskdef (allowing for rollbacks) and 2- are older than let's say the one deployed in the
(latest taskdef - n revisions), giving us some "wiggle_room" during the purging process to account for and skip images that are built but not yet deployed - if you have a continuous delivery pipeline. Below are sample configurations.
```
#################
#PARAMETERS
##################
# NOTE: both wiggle-room and image-age should be set in a way to account for not deleting freshly built images waiting 
# to be deployed (as the ecr-purger purges anything that is not in an active taskdef and taskdefs become active only after succesful deployments)
# ecr-purger inspects all active taskdefs in Prod of that family for that image and only purges images that are not used in any of the active taskdefs; this works on a per repo-basis.
# wiggle-room: (defaults to 20) this is the number of taskdef revisions preceeding the latest active one for every taskdef type where this image is used, allowing us to not delete images that have been built but not yet deployed 
# for instance if an image (i.e builds/<image-name>) is used accross various services then ecr-purger will inspect the respective old 
# taskdefs (latest taskdef - wiggle-room/n revisions) for these services and collect the imagePushedAt time for each version of this 
# container used accross these old taskdef revisions and use the oldest imagePushedAt time found to determine what is to be deleted (precisely anything older than the oldest imagePushedAt time).
# image-age: this is simpler (REQUIRES use-image-age-only: True), it is the age of the image in days to use to determine what images to delete; when using this explicitely set 'use-image-age-only' to 'True'
# i.e. image-age: 30 means 1- ignore wiggle_room and 2- delete everything in the repo that is not in any active taskdef and is more than 30 days old.
# use-image-age-only: False by default, if True then only use image-age
# slack-channels: optional list of slack channels for logging what was deleted to Slack, the same info is in the lambda's log by default. 
##################
FOR EXAMPLE
#################
builds/<image-name>:
  slack-channels: ecr-purger-notifs notif-room2
  image-age: 30
  use-image-age-only: True 
------------------------------------
# Here any builds/<image-name> image that is 1- more than 30 days old and 2- used in an old taskdef (latest taskdef - 20 revisions) will be deleted
# wiggle-room is set to 20 by default to avoid deleting images that are built but not yet deployed and part of an active taskdef.
# wiggle-room can be overridden by any number
#############################################################################
```
# Deploy
This can be deployed as a docker container in ECS per general ecs deployment steps.

## General steps to deploy this as a lambda function using the serverless framework

- Install Serverless Framework (https://serverless.com/framework/docs/providers/aws/guide/installation/)

- Export all nescessary environment variables defined in serverless.yml

```
export env=<ENV>
export securityGroupIds=<SG>
export subnetId1=<SUBNET-ID>
export subnetId2=<SUBNET-ID>
export SLACK_WEBHOOK_URL=<URL>

```

- Install python modules into the .vendor directory 
```
pip install --upgrade -r /path-to/requirements.txt -t .vendor
```

- Deploy
```
serverless deploy --verbose
```
