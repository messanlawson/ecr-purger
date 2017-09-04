"""
Tool for purging ECR repositories
NOTE: ecr-purger's goal is to delete any image that isn't needed anymore from the repo; it attempts to only delete images
that are 1- not in any active taskdef (allowing for rollbacks) and 2- are older than let's say the one deployed in the
(latest taskdef - n revisions), giving us some "wiggle_room" during the purging process to account for and skip images that are built but not yet deployed.
"""

import sys
# making dependencies available ;)
sys.path.insert(0, './.vendor')
import os
import json
import re
import boto3
from yaml import load
from datetime import datetime, timedelta
from pytz import timezone
import slackweb


ecr = boto3.client('ecr', region_name='us-east-1')
ecs = boto3.client('ecs', region_name='us-east-1')

class Repository(object):
    def __init__(self, repo_name, ecs_task_definitions, slack_channels="no-channel", wiggle_room=None, image_age=None, use_image_age_only=False):
        self.name = repo_name
        self.ecs_task_definitions = ecs_task_definitions
        self.slack_channels = None or slack_channels.split()
        self.wiggle_room = wiggle_room
        self.image_age = image_age
        self.use_image_age_only = use_image_age_only

def convert(o):
    """
    helper function to convert datetime objects to __str__ objects for serialization
    """
    if isinstance(o, datetime):
        return o.__str__()

def send_slack(channels, message):
    """
    sends slack message, channels is a list object
    """
    # don't send a slack message if no channel is specified by the user
    if channels[0] == "no-channel":
        return
    webhook_url = os.environ['SLACK_WEBHOOK_URL']
    for channel in channels:
        slack = slackweb.Slack(url=webhook_url)
        try:
            slack_response = slack.notify(text=message, channel=channel, username="ecr-purger", icon_emoji=":lion_face:")
        except Exception as e:
            print "Sending to Slack Failed - Reason:\n", e

        if slack_response != "ok":
            print "Slack Response => ", slack_response

def get_active_task_definition_arns(repository):
    """
    returns dict containing all active taskdef arns per specific taskdef family - where the image is used - i.e.
    {
        "meetup-prod-admin-service": [
            "arn:aws:ecs:us-east-1:212646169882:task-definition/meetup-prod-admin-service:48",
        ],
        "meetup-prod-app-service": [
            "arn:aws:ecs:us-east-1:212646169882:task-definition/meetup-prod-admin-service:48",
        ]
    }
    """
    active_taskdef_arns = {}
    for taskdef in repository.ecs_task_definitions:
        active_taskdef_arns[taskdef] = []
        try:
            response = ecs.list_task_definitions(familyPrefix=taskdef, status='ACTIVE')
            active_taskdef_arns[taskdef].extend(response.get('taskDefinitionArns'))
            while response.get('nextToken', None):
                response = ecs.list_task_definitions(familyPrefix=taskdef, status='ACTIVE', nextToken=response['nextToken'])
                active_taskdef_arns[taskdef].extend(response.get('taskDefinitionArns'))
        except Exception as e:
            print "Error getting list of active task definitions for task definition family '{}'\n".format(taskdef), str(e)
            # re-raise exception, as we need this info
            raise

    return active_taskdef_arns

def get_active_images_details(repository):
    """
    returns a list of images (with details (tags, digest, push-date, etc) that are used in the active taskdefs,
    these images correspond to the specified repository
    """
    active_taskdef_arns = get_active_task_definition_arns(repository)
    active_images_tags = {}
    active_images_tags[repository.name] = []

    for k,v in active_taskdef_arns.iteritems():
        for taskdef in active_taskdef_arns[k]:
            try:
                response = ecs.describe_task_definition(
                    taskDefinition=taskdef
                )
            except Exception as e:
                print "Error describing task definition '{}'\n".format(taskdef), str(e)
                # re-raise exception, as we need this info
                raise
            for container_def in response['taskDefinition']['containerDefinitions']:
                # only pick images corresponding to the repository
                # we want to delete images per repository and not per taskdef 
                if repository.name in container_def['image']:
                    # only grab tagged images
                    try:
                        TAG = container_def['image'].split(':')[1]
                        active_images_tags[repository.name].append(TAG)
                    except IndexError:
                        print "\n*** Warning, Image:'{}' in task definition '{}' has no tag ***".format(container_def['image'], taskdef)
                        print "It's suggested to deregister such taskdef revisions, as our stacks should be using tagged images."
    # need a unique list of TAGs
    active_images_tags[repository.name] = list(set(active_images_tags[repository.name]))

    active_images_details = []
    for tag in active_images_tags[repository.name]:
        try:
            response = ecr.describe_images(
                repositoryName=repository.name,
                imageIds=[
                    {
                        'imageTag': tag
                    },
                ]
            )
            active_images_details.extend(response['imageDetails'])
        except Exception as e:
            print "Error describing image {}:{}\n".format(repository.name, tag), str(e), "\n"
    return active_images_details

def get_age_threshold(repository):
    """
    returns wiggle_room, and the imagePushedAt time in UTC of the image used in (latest taskdef - n revisions) which is used to 
    determine what images to delete vs keep in the repo; wiggle_room is equivalent to x number of preceding revisions
    NOTE: ecr-purger's goal is to delete any image that isn't needed anymore from the repo; it attempts to only delete images
    that are 1- not in any active taskdef (allowing for rollbacks) and 2- are older than let's say the one deployed in the
    (latest taskdef - n revisions), giving us some "wiggle_room" during the purging process to account for and skip images that are built but not yet deployed.
    """

    if repository.wiggle_room is None:
        # set default wiggle_room value
        wiggle_room = 20
    else:
        wiggle_room = repository.wiggle_room

    active_taskdef_arns = get_active_task_definition_arns(repository)

    # get active taskdef revision numbers
    active_taskdef_revs = {}
    for k,v in active_taskdef_arns.iteritems():
        active_taskdef_revs[k] = []
        # only apply wiggle_room when its value is within the range of active taskdefs
        if wiggle_room < len(active_taskdef_arns[k]):
            for arn in active_taskdef_arns[k]:
                active_taskdef_revs[k].append(int(arn.split(':')[-1]))
        else:
            print "No active task definition exists before {} - {} revisions".format(active_taskdef_arns[k][-1], wiggle_room)

    print "Active taskdef revision numbers:\n", json.dumps(active_taskdef_revs, indent=4)

    # find upper limit / revision threshold for each taskdef
    # basically latest taskdef - wiggle_room 
    # wiggle_room ==> n number of preceding revisions
    taskdef_rev_threshold = {}
    for k,v in active_taskdef_revs.iteritems():
        # ensure list of active_taskdef_revs[k] is not empty otherwise
        # ValueError: max() arg is an empty sequence
        if active_taskdef_revs[k]:
            taskdef_rev_threshold[k] = max(active_taskdef_revs[k]) - wiggle_room

    print "Revision thresholds per task definition type:\n", json.dumps(taskdef_rev_threshold, indent=4, default=convert)

    # find push-date of the docker image used in the taskdef corresponding to the
    # taskdef_rev_threshold (latest taskdef - wiggle_room)
    # i.e. taskdef_rev_threshold[k] = 122
    rev_threshold_images_tags = {}
    rev_threshold_images_tags[repository.name] = []

    for k,v in taskdef_rev_threshold.iteritems():
        for taskdef in active_taskdef_arns[k]:
            # find taskdef in active_taskdef_arns corresponding to revision threshold
            if str(v) == str(taskdef.split(':')[-1]):
                try:
                    response = ecs.describe_task_definition(
                        taskDefinition=taskdef
                    )
                except Exception as e:
                    print "Error describing task definition '{}'\n".format(taskdef), str(e)
                    # re-raise exception to halt process, as we need this info
                    raise
                # find tags for the image in this taskdef
                for container_def in response['taskDefinition']['containerDefinitions']:
                    # only pick images corresponding to the repository
                    # we want to delete images per repository and not per taskdef
                    if repository.name in container_def['image']:
                        # only grab tagged images
                        try:
                            TAG = container_def['image'].split(':')[1]
                            rev_threshold_images_tags[repository.name].append(TAG)
                        except IndexError:
                            print "\n*** Image:'{}' in task definition '{}' has no tag, ignoring ***\n".format(container_def['image'], taskdef)
    # need unique list of TAGs
    rev_threshold_images_tags[repository.name] = list(set(rev_threshold_images_tags[repository.name]))

    imagePushedAt_times = []
    for tag in rev_threshold_images_tags[repository.name]:
        try:
            response = ecr.describe_images(
                repositoryName=repository.name,
                imageIds=[
                    {
                        'imageTag': tag
                    },
                ]
            )
        except Exception as e:
            print "Error describing image '{}:{}'\n".format(repository.name, tag), str(e)
            # re-raise exception to halt process, as we need this info
            raise
        imagePushedAt_times.append(response['imageDetails'][0]['imagePushedAt'])
    if imagePushedAt_times:
        # if various TAGs of the same image (i.e. prod-builds/chapstick) are deployed in various taskdefs
        # i.e. app, api, problast-daemon, switchboard taskdefs - then return the imagePushedAt time of the oldest image
        # found accross these taskdefs (that correspond to latest taskdef of each taskdefgroup - wiggle_room)
        print "UTC imagePushedAt time of the image used in (latest taskdef - {} revisions): {}".format(wiggle_room, min(imagePushedAt_times))
        return min(imagePushedAt_times), wiggle_room

    else:
        print "No imagePushedAt_times found:", imagePushedAt_times
        return None, wiggle_room

def get_purgeable_images(repository):
    """
    returns age_threshold plus list of images to be purged, this is a list of dicts containing
    images plus their details: tags, digest, push-date, etc
    """
    # collect metadata on all images in the repo
    images = []
    try:
        response = ecr.describe_images(repositoryName=repository.name)
        images.extend(response.get('imageDetails'))
        while response.get('nextToken', None):
            response = ecr.describe_images(repositoryName=repository.name, nextToken=response['nextToken'])
            images.extend(response.get('imageDetails'))

    except Exception as e:
        print "Error describing all images in repository '{}'\n".format(repository.name), str(e)
        # re-raise exception to halt process, as we need this info
        raise

    # set age_threshold based on use-image-age-only
    if repository.image_age and repository.use_image_age_only is True:
        if not isinstance(repository.image_age, int):
            raise TypeError ('Provide and integer value for image age')
        age_threshold = datetime.now(timezone('UTC')) - timedelta(days=repository.image_age)
        print "*** Using image-age only ({} days ago) ***".format(repository.image_age)
    else:
        # set age_threshold based on wiggle-room
        age_threshold, wiggle_room = get_age_threshold(repository)
        print "*** Using wiggle-room ({} revision ago) ***".format(wiggle_room)

    purgeable_images = []
    active_images_details = get_active_images_details(repository)

    # ensure get_age_threshold returned something
    if age_threshold is not None:
        for image in images:
            # only mark images, in the repo that are not in any active taskdef and
            # are older than the one in (latest taskdef - wiggle_room), as purgeable
            if image not in active_images_details and image['imagePushedAt'] < age_threshold:
                purgeable_images.append(image)

        return purgeable_images, age_threshold

    else:
        return purgeable_images, None


def purge_images(repository):
    """
    gets the list of purgeable_images and purges them
    """
    purgeable_images, age_threshold = get_purgeable_images(repository)

    # ensure we have images to be purged
    if purgeable_images and age_threshold:
        image_list = json.dumps(purgeable_images, indent=4, default=convert)
        purged_msg = "Purged {} image(s), pushed before: {}".format(len(purgeable_images), age_threshold)
        print "Purging {} images from repository '{}':\n {}".format(len(purgeable_images), repository.name, image_list)

        purgeable_imageDigests = []
        for image in purgeable_images:
            purgeable_imageDigests.append(dict(imageDigest=image['imageDigest']))
        try:
            # batch_delete_image only takes up to 100 elements at a time
            for chunk in list(chunks(purgeable_imageDigests, 100)):
                response = ecr.batch_delete_image(
                    repositoryName=repository.name,
                    imageIds=chunk
                )
                # no-op mode
                # print "CHUNK IS ==> ", json.dumps(chunk, indent=4, default=convert)
        except Exception as e:
            print "ecr.batch_delete_image Failed - Reason:\n", str(e)

        send_slack(repository.slack_channels, purged_msg)
        send_slack(repository.slack_channels, image_list)
        print purged_msg
        print image_list
    else:
        print "Not purging anything :(\n"
        print "Purgeable images:\n{}\nAge threshold:\n{}".format(purgeable_images, age_threshold)

def discover_taskdefs(repo_name):
    """
    returns list of taskdefs where an image is used
    """
    # get all active taskdef arns
    active_taskdef_arns = []
    try:
        response = ecs.list_task_definitions(status='ACTIVE')
        active_taskdef_arns.extend(response.get('taskDefinitionArns'))
        while response.get('nextToken', None):
            response = ecs.list_task_definitions(status='ACTIVE', nextToken=response['nextToken'])
            active_taskdef_arns.extend(response.get('taskDefinitionArns'))
    except Exception as e:
        print "Error listing all active task definitions\n", str(e)
        # re-raise exception to halt process, as we need this info
        raise
    # find all taskdef families where this image (corresponding to repo name) is used
    ecs_task_definitions = dict()
    ecs_task_definitions[repo_name] = []

    for taskdef in active_taskdef_arns:
        try:
            response = ecs.describe_task_definition(
                taskDefinition=taskdef
            )
        except Exception as e:
            print "Error describing task definition: {}\n".format(taskdef), str(e)
            # re-raise exception to halt process, as we need this info
            raise

        for container_def in response['taskDefinition']['containerDefinitions']:
            # only grab taskdefs where this image is used
            if repo_name in container_def['image']:
                ecs_task_definitions[repo_name].append(response['taskDefinition']['family'])


    # cleanup dups in ecs_task_definitions
    ecs_task_definitions[repo_name] = list(set(ecs_task_definitions[repo_name]))
    return ecs_task_definitions[repo_name]

def chunks(l, n):
    """
    Divides a list into n-sized chunks
    i.e. to create a list of n-sized chuncks/sub-lists use
    list(chunks(iterable, n))
    """
    # for all items in list
    for i in range(0, len(l), n):
        # yield indexes for each n-sized chunks
        yield l[i:i+n]

def main(event, context):
    """
    main method
    """
    # grab repository names in the ecr-purger's yaml config
    repo_attributes_conf = './repo_attributes.yaml'
    with open(repo_attributes_conf, 'r') as f:
        repositories = load(f)

    # init one repo at a time 
    for repo in repositories.keys():
        # find taskdefs where this image is used
        print "\n******* Inspecting repository: {} *******\n".format(repo)
        ecs_task_definitions = discover_taskdefs(repo)
        print "'{}' is in use in {} task definition(s):\n".format(repo, len(ecs_task_definitions)), json.dumps(ecs_task_definitions, indent=4, default=convert)

        # making slack-channels optional
        if repositories[repo].get('slack-channels') is None:
            slackchannels = "no-channel"
        else:
            slackchannels = repositories[repo].get('slack-channels')

        repository = Repository(
            repo,
            ecs_task_definitions,
            slackchannels,
            repositories[repo].get('wiggle-room'),
            repositories[repo].get('image-age'),
            repositories[repo].get('use-image-age-only'),
        )
        # purge them
        purge_images(repository)
        print "\n******* Done with repository '{}'*******\n".format(repo)

if __name__ == "__main__":
    main(event=None, context=None)
