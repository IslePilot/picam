# picam
A Raspberry Pi camera timelapse photo utility

This also requires the __common project at the same directory level

i.e. your directory structure will look like

<your script directory>/__common
<your script directory>/picam

You can get __common at https://github.com/IslePilot/__common


The timelapse utility will automatically attempt to find the best 
settings for a 24x7 timelapse camera.  It automatically switches
from auto to night mode when it gets dark and back when it gets
light agin.

At this time it only uses sensor_mode 2.  A future improvement could
be to swith to mode 3 at night to increase the ability for very long
exposure times.


