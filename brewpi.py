# Copyright 2012 BrewPi
# This file is part of BrewPi.

# BrewPi is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# BrewPi is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with BrewPi.  If not, see <http://www.gnu.org/licenses/>.

import sys
# Check needed software dependencies to nudge users to fix their setup
if sys.version_info < (2, 7):
	print "Sorry, requires Python 2.7."
	sys.exit(1)

# standard libraries
import time
import socket
import os
import urllib
import getopt
from pprint import pprint
import shutil

# load non standard packages, exit when they are not installed
try:
	import serial
except ImportError:
	print "BrewPi requires PySerial to run, please install it with 'sudo apt-get install python-serial"
	sys.exit(1)
try:
	import simplejson as json
except ImportError:
	print "BrewPi requires simplejson to run, please install it with 'sudo apt-get install python-simplejson"
	sys.exit(1)
try:
	from configobj import ConfigObj
except ImportError:
	print "BrewPi requires ConfigObj to run, please install it with 'sudo apt-get install python-configobj"
	sys.exit(1)



#local imports
import temperatureProfile
import programArduino as programmer
import brewpiJson
import BrewPiUtil as util
from brewpiVersion import AvrInfo
import pinList
import expandLogMessage
import BrewPiProcess



# Settings will be read from Arduino, initialize with same defaults as Arduino
# This is mainly to show what's expected. Will all be overwritten on the first update from the arduino

compatibleBrewpiVersion = "0.2.0"

# Control Settings
cs = dict(mode='b', beerSetting=20.0, fridgeSetting=20.0, heatEstimator=0.2, coolEstimator=5)

# Control Constants
cc = dict(tempFormat="C", tempSetMin=1.0, tempSetMax=30.0, Kp=20.000, Ki=0.600, Kd=-3.000, iMaxErr=0.500,
		  idleRangeH=1.000, idleRangeL=-1.000, heatTargetH=0.301, heatTargetL=-0.199, coolTargetH=0.199,
		  coolTargetL=-0.301, maxHeatTimeForEst="600", maxCoolTimeForEst="1200", fridgeFastFilt="1", fridgeSlowFilt="4",
		  fridgeSlopeFilt="3", beerFastFilt="3", beerSlowFilt="5", beerSlopeFilt="4", lah=0, hs=0)

# Control variables
cv = dict(beerDiff=0.000, diffIntegral=0.000, beerSlope=0.000, p=0.000, i=0.000, d=0.000, estPeak=0.000,
		  negPeakEst=0.000, posPeakEst=0.000, negPeak=0.000, posPeak=0.000)

# listState = "", "d", "h", "dh" to reflect whether the list is up to date for installed (d) and available (h)
deviceList = dict(listState="", installed=[], available=[])

lcdText = ['Script starting up', ' ', ' ', ' ']


def logMessage(message):
	print >> sys.stderr, time.strftime("%b %d %Y %H:%M:%S   ") + message

# Read in command line arguments
try:
	opts, args = getopt.getopt(sys.argv[1:], "hc:sqkfd", ['help', 'config=', 'status', 'quit', 'kill', 'force', 'dontrunfile'])
except getopt.GetoptError:
	print "Available Options: --help, --config <path to config file>, --quit, --kill, --force, --dontrunfile"
	sys.exit()

configFile = None
checkDontRunFile = False

for o, a in opts:
	# print help message for command line options
	if o in ('-h', '--help'):
		print "\n Available command line options: "
		print "--help: print this help message"
		print "--config <path to config file>: specify a config file to use. When omitted settings/config.cf is used"
		print "--status: check which scripts are already running"
		print "--quit: ask all  instances of BrewPi to quit by sending a message to their socket"
		print "--kill: kill all instances of BrewPi by sending SIGKILL"
		print "--force: Force quit/kill conflicting instances of BrewPi and keep this one"
		print "--dontrunfile: check dontrunfile in www directory and quit if it exists"
		exit()
	# supply a config file
	if o in ('-c', '--config'):
		configFile = os.path.abspath(a)
		if not os.path.exists(configFile):
			sys.exit('ERROR: Config file "%s" was not found!' % configFile)
	# send quit instruction to all running instances of BrewPi
	if o in ('-s', '--status'):
		allProcesses = BrewPiProcess.BrewPiProcesses()
		allProcesses.update()
		running = allProcesses.as_dict()
		if running:
			pprint(running)
		else:
			print "No BrewPi scripts running"
		exit()
	# quit/kill running instances, then keep this one
	if o in ('-q', '--quit'):
		logMessage("Asking all BrewPi Processes to quit on their socket")
		allProcesses = BrewPiProcess.BrewPiProcesses()
		allProcesses.quitAll()
		time.sleep(2)
		exit()
	# send SIGKILL to all running instances of BrewPi
	if o in ('-k', '--kill'):
		logMessage("Killing all BrewPi Processes")
		allProcesses = BrewPiProcess.BrewPiProcesses()
		allProcesses.killAll()
		exit()
	# close all existing instances of BrewPi by quit/kill and keep this one
	if o in ('-f', '--force'):
		logMessage("Closing all existing processes of BrewPi and keeping this one")
		allProcesses = BrewPiProcess.BrewPiProcesses()
		if len(allProcesses.update()) > 1:  # if I am not the only one running
			allProcesses.quitAll()
			time.sleep(2)
			if len(allProcesses.update()) > 1:
				print "Asking the other processes to quit nicely did not work. Killing them with force!"
	# only start brewpi when the donotrunfile is not found
	if o in ('-d', '--dontrunfile'):
		checkDontRunFile = True

if not configFile:
	if not checkDontRunFile:  # Do not print when this option is active. CRON uses it and it will flood the logs
		print >> sys.stderr,    ("Using default config path <script dir>/settings/config.cfg, " +
		                         "to override use: %s --config <config file full path>" % sys.argv[0])
	configFile = util.scriptPath() + '/settings/config.cfg'

config = util.readCfgWithDefaults(configFile)

dontRunFilePath = config['wwwPath'] + 'do_not_run_brewpi'
# check dont run file when it exists and exit it it does
if checkDontRunFile:
	if os.path.exists(dontRunFilePath):
		# do not print anything, this will flood the logs
		exit()

# check for other running instances of BrewPi that will cause conflicts with this instance
allProcesses = BrewPiProcess.BrewPiProcesses()
allProcesses.update()
myProcess = allProcesses.me()
if allProcesses.findConflicts(myProcess):
	if not checkDontRunFile:
		logMessage("Another instance of BrewPi is already running, which will conflict with this instance. " +
					"This instance will exit")
	exit(0)

localJsonFileName = ""
localCsvFileName = ""
wwwJsonFileName = ""
wwwCsvFileName = ""
lastDay = ""
day = ""

# wwwSettings.json is a copy of some of the settings for the web server
def changeWwwSetting(settingName, value):
	wwwSettingsFile = open(config['wwwPath'] + 'wwwSettings.json', 'r+b')
	wwwSettings = json.load(wwwSettingsFile)
	wwwSettings[settingName] = value
	wwwSettingsFile.seek(0)
	wwwSettingsFile.write(json.dumps(wwwSettings))
	wwwSettingsFile.truncate()
	wwwSettingsFile.close()


def startBeer(beerName):
	global config
	global localJsonFileName
	global localCsvFileName
	global wwwJsonFileName
	global wwwCsvFileName
	global lastDay
	global day

	# create directory for the data if it does not exist
	dataPath = util.addSlash(config['scriptPath']) + 'data/' + beerName + '/'
	wwwDataPath = util.addSlash(config['wwwPath']) + 'data/' + beerName + '/'

	if not os.path.exists(dataPath):
		os.makedirs(dataPath)
		os.chmod(dataPath, 0775)  # give group all permissions
	if not os.path.exists(wwwDataPath):
		os.makedirs(wwwDataPath)
		os.chmod(wwwDataPath, 0775)  # sudgive group all permissions

	# Keep track of day and make new data tabe for each day
	# This limits data table size, which can grow very big otherwise
	day = time.strftime("%Y-%m-%d")
	lastDay = day
	# define a JSON file to store the data table
	jsonFileName = config['beerName'] + '-' + day
	#if a file for today already existed, add suffix
	if os.path.isfile(dataPath + jsonFileName + '.json'):
		i = 1
		while os.path.isfile(dataPath + jsonFileName + '-' + str(i) + '.json'):
			i += 1
		jsonFileName = jsonFileName + '-' + str(i)
	localJsonFileName = dataPath + jsonFileName + '.json'
	brewpiJson.newEmptyFile(localJsonFileName)

	# Define a location on the web server to copy the file to after it is written
	wwwJsonFileName = wwwDataPath + jsonFileName + '.json'

	# Define a CSV file to store the data as CSV (might be useful one day)
	localCsvFileName = (dataPath + config['beerName'] + '.csv')
	wwwCsvFileName = (wwwDataPath + config['beerName'] + '.csv')
	changeWwwSetting('beerName', beerName)


ser = 0
con = 0
# open serial port
try:
	port = config['port']
	ser = serial.Serial(port, 57600, timeout=2)  # timeout=1 is too slow when waiting on temp sensor reads
except serial.SerialException, e:
	print >> sys.stderr, e
	exit()

dumpSerial = config.get('dumpSerial', False)

# yes this is monkey patching, but I don't see how to replace the methods on a dynamically instantiated type any other way
if dumpSerial:
	ser.readOriginal = ser.read
	ser.writeOriginal = ser.write
	def readAndDump(size=1):
		r = ser.readOriginal(size)
		sys.stdout.write(r)
		return r
	def writeAndDump(data):
		ser.writeOriginal(data)
		sys.stderr.write(data)
	ser.read = readAndDump
	ser.write = writeAndDump


logMessage("Notification: Script started for beer '" + config['beerName'] + "'")
# wait for 10 seconds to allow an Uno to reboot (in case an Uno is being used)
time.sleep(float(config.get('startupDelay', 10)))

ser.flush()
retries = 0
while 1:  # read all lines on serial interface
	line = ser.readline()
	if line:  # line available?
		if line[0] == 'N':
			data = line.strip('\n')[2:]
			avrVersion = AvrInfo(data)
			brewpiVersion = avrVersion.version
			logMessage( "Found Arduino " + str(avrVersion.board) +
						" with a " + str(avrVersion.shield) + " shield, " +
						"running BrewPi version " + str(brewpiVersion) +
						" build " + str(avrVersion.build))
			if brewpiVersion != compatibleBrewpiVersion:
				logMessage("Warning: BrewPi version compatible with this script is " +
						   compatibleBrewpiVersion +
						   " but version number received is " + brewpiVersion)
			if int(avrVersion.log) != int(expandLogMessage.getVersion()):
				logMessage("Warning: version number of local copy of logMessages.h " +
						   "does not match log version number received from Arduino." +
						   "Arduino version = " + str(avrVersion.log) +
						   ", local copy version = " + str(expandLogMessage.getVersion()))
			break
	else:
		ser.write('n')
		time.sleep(1)
		retries += 1
		if retries > 5:
			logMessage("Warning: Cannot receive version number from Arduino. " +
				   "Your Arduino is either not programmed or running a very old version of BrewPi. " +
				   "Please upload a new version of BrewPi to your Arduino.")
			# script will continue so you can at least program the Arduino
			break

ser.flush()
# request settings from Arduino, processed later when reply is received
ser.write('s')  # request control settings cs
ser.write('c') # request control constants cc
# answer from Arduino is received asynchronously later.

# create a listening socket to communicate with PHP
is_windows = sys.platform.startswith('win')
useInetSocket = bool(config.get('useInetSocket', is_windows))
if (useInetSocket):
	s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
	s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
	port = config.get('socketPort', 6332)
	s.bind((config.get('socketHost', 'localhost'), int(port)))
	logMessage('Bound to TCP socket on port %d ' % port)
else:
	socketFile = util.addSlash(config['scriptPath']) + 'BEERSOCKET'
	if os.path.exists(socketFile):
	# if socket already exists, remove it
		os.remove(socketFile)
	s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
	s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
	s.bind(socketFile)  # Bind BEERSOCKET
	# set all permissions for socket
	os.chmod(socketFile, 0777)

serialCheckInterval = 0.5
s.setblocking(1)  # set socket functions to be blocking
s.listen(10)  # Create a backlog queue for up to 10 connections
# blocking socket functions wait 'serialCheckInterval' seconds
s.settimeout(serialCheckInterval)

prevDataTime = 0.0  # keep track of time between new data requests
prevTimeOut = time.time()

run = 1

startBeer(config['beerName'])
outputTemperature = True

prevTempJson = {
"BeerTemp":0,
"FridgeTemp":0,
"BeerAnn":None,
"FridgeAnn":None,
"RoomTemp":None,
"State":None,
"BeerSet":0,
"FridgeSet":0
}
def renameTempKey(key):
	rename = {
	"bt" : "BeerTemp",
	"bs" : "BeerSet",
	"ba":"BeerAnn",
	"ft":"FridgeTemp",
	"fs":"FridgeSet",
	"fa":"FridgeAnn",
	"rt":"RoomTemp",
	"s":"State",
	"t":"Time"
	}
	return rename.get(key, key)

while run:

	# Check whether it is a new day
	lastDay = day
	day = time.strftime("%Y-%m-%d")
	if lastDay != day:
		logMessage("Notification: New day, dropping data table and creating new JSON file.")
		jsonFileName = config['beerName'] + '/' + config['beerName'] + '-' + day
		localJsonFileName = util.addSlash(config['scriptPath']) + 'data/' + jsonFileName + '.json'
		wwwJsonFileName = util.addSlash(config['wwwPath']) + 'data/' + jsonFileName + '.json'
		# create new empty json file
		brewpiJson.newEmptyFile(localJsonFileName)

	# Wait for incoming socket connections.
	# When nothing is received, socket.timeout will be raised after
	# serialCheckInterval seconds. Serial receive will be done then.
	# When messages are expected on serial, the timeout is raised 'manually'
	try:
		conn, addr = s.accept()
		# blocking receive, times out in serialCheckInterval
		message = conn.recv(4096)
		if "=" in message:
			messageType, value = message.split("=", 1)
		else:
			messageType = message
			value = ""
		if messageType == "ack":  # acknowledge request
			conn.send('ack')
		elif messageType == "lcd":  # lcd contents requested
			conn.send(json.dumps(lcdText))
		elif messageType == "getMode":  # echo cs['mode'] setting
			conn.send(cs['mode'])
		elif messageType == "getFridge":  # echo fridge temperature setting
			conn.send(str(cs['fridgeSet']))
		elif messageType == "getBeer":  # echo fridge temperature setting
			conn.send(str(cs['beerSet']))
		elif messageType == "getControlConstants":
			conn.send(json.dumps(cc))
		elif messageType == "getControlSettings":
			conn.send(json.dumps(cs))
		elif messageType == "getControlVariables":
			conn.send(json.dumps(cv))
		elif messageType == "refreshControlConstants":
			ser.write("c")
			raise socket.timeout
		elif messageType == "refreshControlSettings":
			ser.write("s")
			raise socket.timeout
		elif messageType == "refreshControlVariables":
			ser.write("v")
			raise socket.timeout
		elif messageType == "loadDefaultControlSettings":
			ser.write("S")
			raise socket.timeout
		elif messageType == "loadDefaultControlConstants":
			ser.write("C")
			raise socket.timeout
		elif messageType == "setBeer":  # new constant beer temperature received
			newTemp = float(value)
			if cc['tempSetMin'] < newTemp < cc['tempSetMax']:
				cs['mode'] = 'b'
				# round to 2 dec, python will otherwise produce 6.999999999
				cs['beerSet'] = round(newTemp, 2)
				ser.write("j{mode:b, beerSet:" + str(cs['beerSet']) + "}")
				logMessage("Notification: Beer temperature set to " +
						   str(cs['beerSet']) +
						   " degrees in web interface")
				raise socket.timeout  # go to serial communication to update Arduino
			else:
				logMessage("Beer temperature setting " + str(newTemp) +
						   " is outside allowed range " +
						   str(cc['tempSetMin']) + "-" + str(cc['tempSetMax']))
		elif messageType == "setFridge":  # new constant fridge temperature received
			newTemp = float(value)
			if cc['tempSetMin'] < newTemp < cc['tempSetMax']:
				cs['mode'] = 'f'
				cs['fridgeSet'] = round(newTemp, 2)
				ser.write("j{mode:f, fridgeSet:" + str(cs['fridgeSet']) + "}")
				logMessage("Notification: Fridge temperature set to " +
						   str(cs['fridgeSet']) +
						   " degrees in web interface")
				raise socket.timeout  # go to serial communication to update Arduino
		elif messageType == "setProfile":  # cs['mode'] set to profile
			# read temperatures from currentprofile.csv
			cs['mode'] = 'p'
			cs['beerSet'] = temperatureProfile.getNewTemp(config['scriptPath'])
			ser.write("j{mode:p, beerSet:" + str(cs['beerSet']) + "}")
			logMessage("Notification: Profile mode enabled")
			raise socket.timeout  # go to serial communication to update Arduino
		elif messageType == "setOff":  # cs['mode'] set to OFF
			cs['mode'] = 'o'
			ser.write("j{mode:o}")
			logMessage("Notification: Temperature control disabled")
			raise socket.timeout
		elif messageType == "setParameters":
			# receive JSON key:value pairs to set parameters on the Arduino
			try:
				decoded = json.loads(value)
				ser.write("j" + json.dumps(decoded))
				if 'tempFormat' in decoded:
					changeWwwSetting('tempFormat', decoded['tempFormat'])  # change in web interface settings too.
			except json.JSONDecodeError:
				logMessage("Error: invalid JSON parameter string received: " + value)
			raise socket.timeout
		elif messageType == "stopScript":  # exit instruction received. Stop script.
			# voluntary shutdown.
			# write a file to prevent the cron job from restarting the script
			logMessage("stopScript message received on socket. " +
					   "Stopping script and writing dontrunfile to prevent automatic restart")
			run = 0
			dontrunfile = open(dontRunFilePath, "w")
			dontrunfile.write("1")
			dontrunfile.close()
			continue
		elif messageType == "quit":  # quit instruction received. Probably sent by another brewpi script instance
			logMessage("quit message received on socket. Stopping script.")
			run = 0
			# Leave dontrunfile alone.
			# This instruction is meant to restart the script or replace it with another instance.
			continue
		elif messageType == "eraseLogs":
			# erase the log files for stderr and stdout
			open(util.scriptPath() + '/logs/stderr.txt', 'wb').close()
			open(util.scriptPath() + '/logs/stdout.txt', 'wb').close()
			logMessage("Fresh start! Log files erased.")
			continue
		elif messageType == "interval":  # new interval received
			newInterval = int(value)
			if 5 < newInterval < 5000:
				config = util.configSet(configFile, 'interval',float(newInterval))
				logMessage("Notification: Interval changed to " +
						   str(newInterval) + " seconds")
		elif messageType == "name":  # new beer name
			newName = value
			if len(newName) > 3:     # shorter names are probably invalid
				config = util.configSet(configFile, 'beerName', newName)
				startBeer(newName)
				logMessage("Notification: restarted for beer: " + newName)
		elif messageType == "profileKey":
			config = util.configSet(configFile, 'profileKey', value)
			changeWwwSetting('profileKey', value)
		elif messageType == "uploadProfile":
			# use urllib to download the profile as a CSV file
			profileUrl = ("https://spreadsheets.google.com/tq?key=" +
						  config['profileKey'] +
						  "&tq=select D,E&tqx=out:csv")  # select the right cells and CSV format
			profileFileName = config['scriptPath'] + 'settings/tempProfile.csv'
			if os.path.isfile(profileFileName + '.old'):
				os.remove(profileFileName + '.old')
			os.rename(profileFileName, profileFileName + '.old')
			urllib.urlretrieve(profileUrl, profileFileName)
			if os.path.isfile(profileFileName):
				conn.send("Profile successfuly updated")
			else:
				conn.send("Failed to update profile")
		elif messageType == "programArduino":
			ser.close()  # close serial port before programming
			del ser  # Arduino won't reset when serial port is not completely removed
			try:
				programParameters = json.loads(value)
				hexFile = programParameters['fileName']
				boardType = programParameters['boardType']
				restoreSettings = programParameters['restoreSettings']
				restoreDevices = programParameters['restoreDevices']
				programmer.programArduino(config, boardType, hexFile, {'settings': restoreSettings, 'devices': restoreDevices})
				logMessage("New program uploaded to Arduino, script will restart")
			except json.JSONDecodeError:
				logMessage("Error: cannot decode programming parameters: " + value)
				logMessage("Restarting script without programming.")

			# restart the script when done. This replaces this process with the new one
			time.sleep(5)  # give the Arduino time to reboot
			python = sys.executable
			os.execl(python, python, *sys.argv)
		elif messageType == "refreshDeviceList":
			deviceList['listState'] = ""  # invalidate local copy
			if value.find("readValues") != -1:
				ser.write("d{r:1}")  # request installed devices
				ser.write("h{u:-1,v:1}")  # request available, but not installed devices
			else:
				ser.write("d{}")  # request installed devices
				ser.write("h{u:-1}")  # request available, but not installed devices
		elif messageType == "getDeviceList":
			if deviceList['listState'] in ["dh", "hd"]:
				response = dict(board=avrVersion.board,
							shield=avrVersion.shield,
							deviceList=deviceList,
							pinList=pinList.getPinList(avrVersion.board, avrVersion.shield))
				conn.send(json.dumps(response))
			else:
				conn.send("device-list-not-up-to-date")
		elif messageType == "applyDevice":
			try:
				configStringJson = json.loads(value)  # load as JSON to check syntax
			except json.JSONDecodeError:
				logMessage("Error: invalid JSON parameter string received: " + value)
				continue
			ser.write("U" + value)
			deviceList['listState'] = ""  # invalidate local copy
		else:
			logMessage("Error: Received invalid message on socket: " + message)

		if (time.time() - prevTimeOut) < serialCheckInterval:
			continue
		else:
			# raise exception to check serial for data immediately
			raise socket.timeout

	except socket.timeout:
		# Do serial communication and update settings every SerialCheckInterval
		prevTimeOut = time.time()

		# request new LCD text
		ser.write('l')
		# request Settings from Arduino to stay up to date
		ser.write('s')

		# if no new data has been received for serialRequestInteval seconds
		if (time.time() - prevDataTime) >= float(config['interval']):
			ser.write("t")  # request new from arduino

		elif (time.time() - prevDataTime) > float(config['interval']) + 2 * float(config['interval']):
			#something is wrong: arduino is not responding to data requests
			logMessage("Error: Arduino is not responding to new data requests")

		while 1:  # read all lines on serial interface
			line = ser.readline()
			if line:  # line available?
				try:
					if line[0] == 'T':

						# print it to stdout
						if outputTemperature:
							print time.strftime("%b %d %Y %H:%M:%S  ") + line[2:]
						# process temperature line
						newData = json.loads(line[2:])
						# copy/rename keys
						for key in newData:
							prevTempJson[renameTempKey(key)] = newData[key]

						newRow = prevTempJson
						# add to JSON file
						brewpiJson.addRow(localJsonFileName, newRow)
						# copy to www dir.
						# Do not write directly to www dir to prevent blocking www file.
						shutil.copyfile(localJsonFileName, wwwJsonFileName)
						#write csv file too
						csvFile = open(localCsvFileName, "a")
						try:
							lineToWrite = (time.strftime("%b %d %Y %H:%M:%S;") +
										   str(newRow['BeerTemp']) + ';' +
										   str(newRow['BeerSet']) + ';' +
										   str(newRow['BeerAnn']) + ';' +
										   str(newRow['FridgeTemp']) + ';' +
										   str(newRow['FridgeSet']) + ';' +
										   str(newRow['FridgeAnn']) + ';' +
										   str(newRow['State']) + ';' +
										   str(newRow['RoomTemp']) + '\n')
							csvFile.write(lineToWrite)
						except KeyError, e:
							logMessage("KeyError in line from Arduino: %s" % e)

						csvFile.close()
						shutil.copyfile(localCsvFileName, wwwCsvFileName)
						# store time of last new data for interval check
						prevDataTime = time.time()
					elif line[0] == 'D':
						# debug message received
						try:
							expandedMessage = expandLogMessage.expandLogMessage(line[2:])
							logMessage("Arduino debug message: " + expandedMessage)
						except Exception, e:  # catch all exceptions, because out of date file could cause errors
							logMessage("Error while expanding log message '" + line[2:] + "'" + str(e))

					elif line[0] == 'L':
						# lcd content received
						lcdTextReplaced = line[2:].replace('\xb0', '&deg')  # replace degree sign with &deg
						lcdText = json.loads(lcdTextReplaced)
					elif line[0] == 'C':
						# Control constants received
						cc = json.loads(line[2:])
					elif line[0] == 'S':
						# Control settings received
						cs = json.loads(line[2:])
					# do not print this to the log file. This is requested continuously.
					elif line[0] == 'V':
						# Control settings received
						cv = json.loads(line[2:])
					elif line[0] == 'N':
						pass  # version number received. Do nothing, just ignore
					elif line[0] == 'h':
						deviceList['available'] = json.loads(line[2:])
						oldListState = deviceList['listState']
						deviceList['listState'] = oldListState.strip('h') + "h"
						logMessage("Available devices received: " + str(deviceList['available']))
					elif line[0] == 'd':
						deviceList['installed'] = json.loads(line[2:])
						oldListState = deviceList['listState']
						deviceList['listState'] = oldListState.strip('d') + "d"
						logMessage("Installed devices received: " + str(deviceList['installed']))
					elif line[0] == 'U':
						logMessage("Device updated to: " + line[2:])
					else:
						logMessage("Cannot process line from Arduino: " + line)
					# end or processing a line
				except json.decoder.JSONDecodeError, e:
					logMessage("JSON decode error: %s" % e)
					logMessage("Line received was: " + line)
			else:
				# no lines left to process
				break

		# Check for update from temperature profile
		if cs['mode'] == 'p':
			newTemp = temperatureProfile.getNewTemp(config['scriptPath'])
			if cc['tempSetMin'] < newTemp < cc['tempSetMax']:
				if newTemp != cs['beerSet']:
					# if temperature has to be updated send settings to arduino
					cs['beerSet'] = temperatureProfile.getNewTemp(config['scriptPath'])
					ser.write("j{beerSet:" + str(cs['beerSet']) + "}")

	except socket.error, e:
		logMessage("socket error: %s" % e)

if ser:
	ser.close()  # close port
if conn:
	conn.shutdown(socket.SHUT_RDWR)  # close socket
	conn.close()
