# StormBench 1.0

Copyright Kenneth Falck <kennu@iki.fi> 2012.
See LICENSE for copyright information (BSD license).

## Overview

StormBench is a distributed web load testing system for Amazon AWS.
It launch a number of EC2 instances and runs ApacheBench on each of
them to perform a distributed load test. The results are stored in
a central Redis server, which also coordinates the clients so they
start their benchmarks at the same time. At the end, StormBench
reads the results and combines them to a single report.

StormBench will tag all resources it uses on EC2 with the tag
"StormBench:True". This tag is used to clean them up afterwards,
even if a test run or image creation is aborted. Additionally, the
image names and Name tags of all resources will use a common prefix
(specified with the -p option), which helps you identify the resources
in the AWS Console.

## Usage

StormBench is a Python script which performs all the necessary operations.
The main functionalities are documented here. You can also run the script
to get usage information.

### Common options

All stormbench.py commands accept these options:

    -k
    --key                 AWS access key to use. REQUIRED
    -s
    --secret              AWS secret access key to use. REQUIRED
    -r us-east-1
    --region us-east-1    AWS region to use. Default: eu-west-1
    -p tag-prefix
    --prefix tag-prefix   AWS tag prefix. Default: stormbench

### Creating an Amazon EC2 AMI image

A custom AMI image is prepared so that each node of the distributed test
cluster can start up automatically and independently. The AMI image is
based on the official Ubuntu 12.04 LTS 32-bit AMI:

http://cloud-images.ubuntu.com/releases/precise/release/

The preparation phase will launch an EC2 instance in the specified EC2
region, install all available updates, install Redis and ab, and set up
the necessary init scripts. A new AMI is then created and the temporary
micro instance is terminated.

Command line usage:

    ./stormbench.py [options] createimage

Options:

    -b ami-c7aaabb3
    --base ami-c7aaabb3   Base AMI image. This will default to the
                          Ubuntu 12.04 EBS 32-bit AMI for the active
                          region.

### Performing a distributed load test

Command line usage:

    ./stormbench.py [options] benchmark <url>

Arguments:

    <url>                 The URL to test. REQUIRED

Options:

    -i 100
    --instances 100       Number of client instances to start. Default: 1
    -n 1000
    --numrequests         Number of requests/client to make. Default: 1
    -c 50
    --concurrency 50      Concurrency of each client. Default: 1
    -o <options>
    --options <options>   Specify additional options for ApacheBench.
                          See man ab(1) for more information.

### Checking the resource usage status

This command will show the Amazon EC2 resources that are currently used
by StormBench. The status display is based on the StormBench:True tag.

Command line usage:

    ./stormbench.py [options] status

### Cleaning up resources used by StormBench

This command will clean up all Amazon EC2 resources that have been launched
by StormBench. They are identified by the StormBench:True tag.

Command line usage:

    ./stormbench.py [options] cleanup

### About the central Redis server

StormBench requires one centralized Redis server for all distributed
nodes to connect to. It will be started automatically whenever needed.
If you have created a custom AMI image, the image will be used to
launch the server. Otherwise a virgin Ubuntu AMI will be used.

Note that all EC2 instances must be able to contact the server at the
specified address. The server is used to synchronize the startup time
of the test and collect the results from all nodes.

