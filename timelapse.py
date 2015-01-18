#!/usr/bin/python

"""
Copyright (C) 2015 AeroSys Engineering, Inc.

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.

Revision History:
  2015-01-17, ksb, created
"""


import time
import datetime
import signal
import os
import threading

import picamera
import numpy as np
import io
import Image
from fractions import Fraction

import sys
sys.path.append("..")
import __common.ftp_client as ftp_client
import __common.file_tools as file_tools

# define a version for this file
VERSION = "1.0.20150117a"

def signal_handler(signal, frame):
  print "You pressed Control-c.  Exiting."
  sys.exit(0)
signal.signal(signal.SIGINT, signal_handler)

class Averager(object):
  """This class will keep track of previous setting configurations
  and will return average values to slow setting changes."""
  def __init__(self, history_max=5):
    self.history_max = history_max
    self.reset()

  def reset(self):
    self.es = []
    self.awb_gains = []
    self.brightness = []

  def add_exposure_speed(self, val):
    # append this value
    self.es.append(val)
    
    # maintain our FIFO history
    if len(self.es) > self.history_max:
      # we are full, delete the oldest
      del(self.es[0])
          
  def add_awb_gains(self, val):
    # append this value
    self.awb_gains.append(val)
    
    # maintain our FIFO history
    if len(self.awb_gains) > self.history_max:
      # we are full, delete the oldest
      del(self.awb_gains[0])
          
  def add_brightness(self, val):
    # append this value
    self.brightness.append(val)
    
    # maintain our FIFO history
    if len(self.brightness) > self.history_max:
      # we are full, delete the oldest
      del(self.brightness[0])

  def average_exposure_speed(self):
    return np.mean(self.es)

  def average_awb(self):
    return np.mean(self.awb_gains, axis=0)

  def average_brightness(self):
    return np.mean(self.brightness)

class TimeLapse(object):
  """This class controls the Raspberry Pi camera for slow changes in settings
  thus allowing it to be used for long term timelapse photography.

  Considerable information and recipes have been gleaned from the picamera
  module documentation at http://picamera.readthedocs.org/en/latest/index.html.

  Additional inspiration and ideas from the code presented by Tom Denton at
  http://inventingsituations.net/2014/01/01/pilapse3/.  I tried to get his
  code to work but ran into many issues so I decided to start over.  His
  work was invaluable, however in my getting started in the right direction."""

  def __init__(self, path, interval, ftp_on=False):
    # The sensor mode determines the maximum and minimum framerates.
    # Mode 2 allows frame rates between 1/1 and 15/1.
    # Mode 3 allows frame rates between 1/6 and 1/1.
    # Mode 2 is useful for daytime, Mode 2 is useful for low-light situations
    # The framerate matters because the shutter speed is limited by the framerate.
    # Start off assuming it is daytime
    self.sensor_mode = 2
    self.framerate = 1
    self.exposure_mode = 'auto'

    # Instance the PiCamera
    # All images will be captured via the image port which always uses 2592x1944.
    self.camera = picamera.PiCamera(resolution=(2592, 1944),
                                    framerate=self.framerate,
                                    sensor_mode=self.sensor_mode)

    # Instance the Averager
    self.averager = Averager(history_max=5)

    # save the path
    self.path = path
    self.default_filename = "{:s}/image.jpg".format(self.path)

    # save the ftp_flag
    self.ftp_on = ftp_on

    # create the timer semphore, as we only want to run one at a time
    self.timer_semaphore = threading.Semaphore(1)

    # get the initial settings
    for i in range(3):
      print "Beginning Tuning {:d} of 3".format(i+1)
      self.get_auto_settings()
      self.averager.add_exposure_speed(self.camera.exposure_speed)    
      self.averager.add_awb_gains(self.camera.awb_gains)    
      self.capture('try.jpg')
      self.set_exposure_mode()

    # get to zero seconds
    timenow = datetime.datetime.now()
    wait_sec = 60 - timenow.second
    print timenow
    print "Waiting {:d} seconds for the top of the minute.".format(wait_sec)
    
    # we are going to delay 1 second when we start the timer, so subtract 1
    if wait_sec >= 2:
      time.sleep(wait_sec-1)

    # start our timer to go off every requested interval
    signal.setitimer(signal.ITIMER_REAL, 1.0, interval)
    signal.signal(signal.SIGALRM, self.timer_isr)

    return

  def timer_isr(self, signal, frame):
    """This will automatically be called every interval seconds."""

    # get the semaphore...if we can't get it,  skip this acquisition, don't block
    if self.timer_semaphore.acquire(False) == False:
      return

    # get our timestamp and filename
    timenow = time.localtime()
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S %Z", timenow)
    filename = "{:s}/{:s}.jpg".format(self.path, time.strftime("%Y%m%d_%H%M%S_%Z", timenow))

    # let the camera settle on its automatic settings
    self.get_auto_settings()

    # add these settings to our history
    self.averager.add_exposure_speed(self.camera.exposure_speed)    
    self.averager.add_awb_gains(self.camera.awb_gains)    

    # now take the picture
    self.capture(filename)

    # set the auto mode
    self.set_exposure_mode()

    # add the timestamp
    self.add_timestamp(timestamp, filename)

    # copy to a timestamped filename
    file_tools.copy_file(filename, self.default_filename) 

    # ftp the data to Wunderground
    if self.ftp_on:
      self.ftp_file()

    # print that we are done
    print "waiting..."

    # release the semaphore
    self.timer_semaphore.release()


  def get_auto_settings(self):
    """Get the current settings the camera thinks are correct"""
    # move to automatic mode
    self.camera.iso = 0                 # automatic, required to allow exposure_mode to work
    self.camera.shutter_speed = 0       # automatic
    self.camera.exposure_mode = self.exposure_mode
    self.camera.awb_mode = 'auto'       # automatic

    # wait for the camera to settle down
    count = 0
    last_es = -1
    while abs(self.camera.exposure_speed - last_es) > 0 or self.camera.exposure_speed == 0:
      print "Diff: ", abs(self.camera.exposure_speed - last_es)
      count += 1
      print "Waiting Count: ", count      

      # if we get stuck in here, call it good enough
      if count > 5:
        break

      # save the current speed
      last_es = self.camera.exposure_speed

      time.sleep(5)

    # show the user what we will use
    self.print_current_settings()

  def set_exposure_mode(self):
    # find the current exposure speed and mode
    es = self.camera.exposure_speed
    mode = self.camera.exposure_mode

    print "Current Exposure Mode: {:s}".format(mode)

    if es > 62000 and mode == 'auto':
      print "Setting Exposure Mode to 'night'"
      self.exposure_mode = 'night'
    elif es < 62000 and mode == 'night':
      print "Setting Exposure Mode to 'auto'"
      self.exposure_mode = 'auto'

  def print_current_settings(self):
    # show the user what we will use
    print "Automated Settings:"
    print "  exposure_speed: ", self.camera.exposure_speed
    print "      resolution: ", self.camera.resolution
    print "       awb_gains: ", self.camera.awb_gains
    print "     Analog Gain: ", self.camera.analog_gain
    print "    Digital Gain: ", self.camera.digital_gain
    
  def capture(self, filename):
    """Capture a PIL image with the current settings."""
    # create an in-memory stream
    stream = io.BytesIO()

    # capture the stream, set our desired resolution to use the Pi GPU
    self.camera.capture(stream, format='jpeg', resize=(1440, 1080))

    # "Rewind" the stream so we can read its contents
    stream.seek(0)

    # convert to a PIL Image
    image = Image.open(stream)

    # save the file
    image.save(filename)
 
    # compute the images brightness
    brightness = self.compute_brightness(image=image, zone='all')
    self.averager.add_brightness(brightness)
    print "Brightness: Current:{:.2f} Average:{:.2f}".format(brightness, self.averager.average_brightness())

    # close the stream
    stream.close()
    
  def compute_brightness(self, image, zone='all'):
    """Find the average brightness of the provided PIL image

    image: a PIL image

    returns: brightness"""
    # first convert the image to greyscale
    im = image.convert("L")

    # find the size to scale the meter window
    height, width = im.size

    # find the pixel position of our window
    if zone == 'center':
      # use 30% of the frame, centered in the middle
      top = int(float(height)/2.0 - 0.15*float(height))+1
      bottom = int(float(height)/2.0 + 0.15*float(height))-1
      left = int(float(width)/2.0 - 0.15*float(width))+1
      right = int(float(width)/2.0 + 0.15*float(width))-1

      # crop the image
      im = im.crop((left, top, right, bottom))

    # compute the number of pixels
    pixels = im.size[0] * im.size[1]

    # generate a histogram of the greyscale colors
    h = im.histogram()
   
    # normalize the histogram
    h = [float(i)/pixels for i in h]

    # the histogram now shows the normalized "power" of each
    # greyscale bin.  Compute a weighted average of the bins
    # by multiplying the bin power by its position.
    # bin zero is black (and adds nothing to brightness)
    # bin(len) is white and adds full power
    return sum([i*h[i] for i in range(len(h))])

  def add_timestamp(self, timestamp, filename):
    # place timestamp on the image
    cmd = "convert"
    cmd += " {:s}".format(filename)
    cmd += " -font fixed -pointsize 50"
    cmd += " -draw \"gravity southwest "
    cmd += " fill black text 0,12 '{:s}'".format(timestamp)
    cmd += " fill white text 1,11 '{:s}'\"".format(timestamp)
    cmd += " {:s}".format(filename)
    print "Adding timestamp..."

    # run the command
    try:
      os.system("{:s}".format(cmd))
    except:
      print "unable to add timestamp"
      print "Unexpected error:", sys.exc_info()[0]
    

  def ftp_file(self):
    # get the username and password
    # the file contains one line with the username and password separated by a space
    login = open('/home/pi/py_scripts/picam/.wunderground.txt', 'r')
    contents = login.read()
    login.close
    data = contents.split()
    username = data[0]
    password = data[1]
      
    # ftp the file to Wunderground
    server = 'webcam.wunderground.com'
    user = username
    pw = password

    ftp = ftp_client.FTP_Client(server, user, pw)
    ftp.binary_put(self.default_filename)
    ftp.disconnect()


def main():
  print("Copyright (C) 2015 AeroSys Engineering, Inc.")
  print("This program comes with ABSOLUTELY NO WARRANTY;")
  print("This is free software, and you are welcome to redistribute it")
  print("under certain conditions.  See GNU Public License.")
  print("")

  print("Press Control-c to exit.")

  # instance the TimeLapse class
  tl = TimeLapse(path='/mnt/keith-pc/timelapse', interval=60, ftp_on=True)

  # wait here forever
  while True:
    time.sleep(10)

# only run main if this is called directly
if __name__ == '__main__':
  main()

