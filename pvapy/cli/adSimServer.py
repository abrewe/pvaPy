#!/usr/bin/env python

import time
import random
import threading
import queue
import argparse
import os
import os.path
import numpy as np
# HDF5 is optional
try:
    import h5py as h5
except ImportError:
    h5 = None

import pvaccess as pva
from ..utility.adImageUtility import AdImageUtility
from ..utility.floatWithUnits import FloatWithUnits
from ..utility.intWithUnits import IntWithUnits

__version__ = pva.__version__

class AdSimServer:

    # Uses frame cache of a given size. If the number of input
    # files is larger than the cache size, the server will be constantly 
    # regenerating frames.

    SHUTDOWN_DELAY = 1.0
    MIN_CACHE_SIZE = 1
    CACHE_TIMEOUT = 1.0
    DELAY_CORRECTION = 0.0001
    NOTIFICATION_DELAY = 0.1
    BYTES_IN_MEGABYTE = 1000000
    METADATA_TYPE_DICT = {
        'value' : pva.DOUBLE,
        'timeStamp' : pva.PvTimeStamp()
    }

    def __init__(self, inputDirectory, inputFile, mmapMode, hdfDataset, frameRate, nFrames, cacheSize, nx, ny, datatype, minimum, maximum, runtime, channelName, notifyPv, notifyPvValue, metadataPv, startDelay, reportPeriod, disableCurses):
        self.lock = threading.Lock()
        self.deltaT = 0
        self.cacheTimeout = self.CACHE_TIMEOUT
        if frameRate > 0:
            self.deltaT = 1.0/frameRate
            self.cacheTimeout = max(self.CACHE_TIMEOUT, self.deltaT)
        self.runtime = runtime
        self.reportPeriod = reportPeriod 
        self.metadataIoc = None
        self.frameSourceList = []
        self.frameCacheSize = max(cacheSize, self.MIN_CACHE_SIZE)
        self.nFrames = nFrames

        inputFiles = []
        if inputDirectory is not None:
            inputFiles = [os.path.join(inputDirectory, f) for f in os.listdir(inputDirectory) if os.path.isfile(os.path.join(inputDirectory, f))]
        if inputFile is not None:
            inputFiles.append(inputFile)
        for f in inputFiles:
            frames = self.loadInputFile(f, mmapMode=mmapMode, hdfDatasetPath=hdfDataset)
            if frames is not None:
                self.frameSourceList.append(frames)

        if not self.frameSourceList:
            nf = nFrames
            if nf <= 0:
                nf = self.frameCacheSize
            frames = self.generateFrames(nf, nx, ny, datatype, minimum, maximum)
            self.frameSourceList.append(frames)

        self.nInputFrames = 0
        for fs in self.frameSourceList:
            nInputFrames, self.rows, self.cols = fs.shape
            self.nInputFrames += nInputFrames
        if self.nFrames > 0:
            self.nInputFrames = min(self.nFrames, self.nInputFrames)

        frames = self.frameSourceList[0]

        self.frameRate = frameRate
        self.imageSize = IntWithUnits(self.rows*self.cols*frames[0].itemsize, 'B')
        self.expectedDataRate = FloatWithUnits(self.imageSize*self.frameRate/self.BYTES_IN_MEGABYTE, 'MBps')

        self.channelName = channelName
        self.pvaServer = pva.PvaServer()
        self.setupMetadataPvs(metadataPv)
        self.pvaServer.addRecord(self.channelName, pva.NtNdArray(), None)

        if notifyPv and notifyPvValue:
            try:
                time.sleep(self.NOTIFICATION_DELAY)
                notifyChannel = pva.Channel(notifyPv, pva.CA)
                notifyChannel.put(notifyPvValue)
                print(f'Set notification PV {notifyPv} to {notifyPvValue}')
            except Exception as ex:
                print(f'Could not set notification PV {notifyPv} to {notifyPvValue}: {ex}')

        # Use PvObjectQueue if cache size is too small for all input frames
        # Otherwise, simple dictionary is good enough
        self.usingQueue = False
        if self.nInputFrames > self.frameCacheSize:
            self.usingQueue = True
            self.frameCache = pva.PvObjectQueue(self.frameCacheSize)
        else:
            self.frameCache = {}

        print(f'Number of input frames: {self.nInputFrames} (size: {self.cols}x{self.rows}, {self.imageSize}, type: {frames.dtype})')
        print(f'Frame cache type: {type(self.frameCache)} (cache size: {self.frameCacheSize})')
        print(f'Expected data rate: {self.expectedDataRate}')

        self.currentFrameId = 0
        self.nPublishedFrames = 0
        self.startTime = 0
        self.lastPublishedTime = 0
        self.startDelay = startDelay
        self.isDone = False
        self.screen = None
        self.screenInitialized = False
        self.disableCurses = disableCurses

    def setupCurses(self):
        screen = None
        if not self.disableCurses:
            try:
                import curses
                screen = curses.initscr()
                self.curses = curses
            except ImportError as ex:
                pass
        return screen

    def loadInputFile(self, filePath, mmapMode=False, hdfDatasetPath=None):
        try:
            frames = None
            allowedHdfExtensions = ['.h5', '.hdf', '.hdf5']
            isHdf = False
            for ext in allowedHdfExtensions:
                if filePath.endswith(ext):
                    isHdf = True
                    break
            if isHdf:
                if not h5:
                    raise Exception(f'Missing HDF support.')
                if not hdfDatasetPath:
                    raise Exception(f'Missing HDF dataset specification for input file {filePath}.')
                hdfFile = h5.File(filePath, 'r')
                hdfDataset = hdfFile[hdfDatasetPath]
                frames = hdfDataset
            else:
                if mmapMode:
                    frames = np.load(filePath, mmapMode='r')
                else:
                    frames = np.load(filePath)
            print(f'Loaded input file {filePath}')
        except Exception as ex:
            print(f'Cannot load input file {filePath}, skipping it: {ex}')
        return frames

    def generateFrames(self, nf, nx, ny, datatype, minimum, maximum):
        print('Generating random frames')
        # Example frame:
        # frame = np.array([[0,0,0,0,0,0,0,0,0,0],
        #                  [0,0,0,0,1,1,0,0,0,0],
        #                  [0,0,0,1,2,3,2,0,0,0],
        #                  [0,0,0,1,2,3,2,0,0,0],
        #                  [0,0,0,1,2,3,2,0,0,0],
        #                  [0,0,0,0,0,0,0,0,0,0]], dtype=np.uint16)
        dt = np.dtype(datatype)
        if datatype != 'float32' and datatype != 'float64':
            dtinfo = np.iinfo(dt)
            mn = dtinfo.min
            if minimum is not None:
                mn = int(max(dtinfo.min, minimum))
            mx = dtinfo.max
            if maximum is not None:
                mx = int(min(dtinfo.max, maximum))
            frames = np.random.randint(mn, mx, size=(nf, ny, nx), dtype=dt)
        else:
            # Use float32 for min/max, to prevent overflow errors
            dtinfo = np.finfo(np.float32)
            mn = dtinfo.min
            if minimum is not None:
                mn = float(max(dtinfo.min, minimum))
            mx = dtinfo.max
            if maximum is not None:
                mx = float(min(dtinfo.max, maximum))
            frames = np.random.uniform(mn, mx, size=(nf, ny, nx))
            if datatype == 'float32':
                frames = np.float32(frames)

        print(f'Generated frame shape: {frames[0].shape}')
        print(f'Range of generated values: [{mn},{mx}]')
        return frames

    def setupMetadataPvs(self, metadataPv):
        self.caMetadataPvs = []
        self.pvaMetadataPvs = []
        self.metadataPvs = []
        if not metadataPv:
            return
        mPvs = metadataPv.split(',')
        for mPv in mPvs:
            if not mPv:
                continue

            # Assume CA is the default protocol
            if mPv.startswith('pva://'):
                self.pvaMetadataPvs.append(mPv.replace('pva://', ''))
            else:
                self.caMetadataPvs.append(mPv.replace('ca://', ''))
        self.metadataPvs = self.caMetadataPvs+self.pvaMetadataPvs
        if self.caMetadataPvs:
            if not os.environ.get('EPICS_DB_INCLUDE_PATH'):
                print(f'EPICS_DB_INCLUDE_PATH should point to EPICS BASE dbd directory for CA metadata support')   
                self.caMetadataPvs = []

        print(f'CA Metadata PVs: {self.caMetadataPvs}')
        if self.caMetadataPvs:
            # Create database and start CA IOC
            import tempfile
            dbFile = tempfile.NamedTemporaryFile(delete=False) 
            dbFile.write(b'record(ao, "$(NAME)") {}\n')
            dbFile.close()

            self.metadataIoc = pva.CaIoc()
            self.metadataIoc.loadDatabase('base.dbd', '', '')
            self.metadataIoc.registerRecordDeviceDriver()
            for mPv in self.caMetadataPvs: 
                print(f'Creating CA metadata record: {mPv}')
                self.metadataIoc.loadRecords(dbFile.name, f'NAME={mPv}')
            self.metadataIoc.start()
            os.unlink(dbFile.name)

        print(f'PVA Metadata PVs: {self.pvaMetadataPvs}')
        if self.pvaMetadataPvs:
            for mPv in self.pvaMetadataPvs: 
                print(f'Creating PVA metadata record: {mPv}')
                mPvObject = pva.PvObject(self.METADATA_TYPE_DICT)
                self.pvaServer.addRecord(mPv, mPvObject, None)

    def getMetadataValueDict(self):
        metadataValueDict = {}
        for mPv in self.metadataPvs: 
            value = random.uniform(0,1)
            metadataValueDict[mPv] = value
        return metadataValueDict

    def updateMetadataPvs(self, metadataValueDict):
        # Returns time when metadata is published
        # For CA metadata will be published before data timestamp
        # For PVA metadata will have the same timestamp as data
        for mPv in self.caMetadataPvs:
            value = metadataValueDict.get(mPv)
            self.metadataIoc.putField(mPv, str(value))
        t = time.time()
        for mPv in self.pvaMetadataPvs:
            value = metadataValueDict.get(mPv)
            mPvObject = pva.PvObject(self.METADATA_TYPE_DICT, {'value' : value, 'timeStamp' : pva.PvTimeStamp(t)})
            self.pvaServer.update(mPv, mPvObject)
        return t
        
    def addFrameToCache(self, frameId, ntnda):
        if not self.usingQueue:
            # Using dictionary
            self.frameCache[frameId] = ntnda
        else:
            # Using PvObjectQueue
            try:
                self.frameCache.put(ntnda, self.cacheTimeout)
            except pva.QueueFull:
                pass
            
    def getFrameFromCache(self):
        if not self.usingQueue:
            # Using dictionary
            cachedFrameId = self.currentFrameId % self.nInputFrames
            if cachedFrameId not in self.frameCache:
            # In case frames were not generated on time, just use first frame
                cachedFrameId = 0
            ntnda = self.frameCache[cachedFrameId]
        else:
            # Using PvObjectQueue
            ntnda = self.frameCache.get(self.cacheTimeout)
        return ntnda

    def frameProducer(self, extraFieldsPvObject=None):
        startTime = time.time()
        frameId = 0
        while not self.isDone:
            for frames in self.frameSourceList:
                for frame in frames:
                    if self.isDone or (self.nFrames > 0 and frameId >= self.nFrames):
                        break
                    ntnda = AdImageUtility.generateNtNdArray2D(frameId, frame, extraFieldsPvObject)
                    self.addFrameToCache(frameId, ntnda)
                    frameId += 1
            if not self.usingQueue:
                # All frames are in cache
                break
        self.printReport(f'Frame producer is done after {frameId} generated frames')

    def prepareFrame(self, t=0):
        # Get cached frame
        frame = self.getFrameFromCache()

        # Correct image id and timestamps
        self.currentFrameId += 1
        frame['uniqueId'] = self.currentFrameId
        if t <= 0:
            t = time.time()
        ts = pva.PvTimeStamp(t)
        frame['timeStamp'] = ts
        frame['dataTimeStamp'] = ts
        return frame

    def framePublisher(self):
        while True:
            if self.isDone:
                return

            # Prepare metadata
            metadataValueDict = self.getMetadataValueDict()

            # Update metadata and take timestamp
            updateTime = self.updateMetadataPvs(metadataValueDict)

            # Prepare frame with a given timestamp
            # so that metadata and image times are as close as possible
            try:
                frame = self.prepareFrame(updateTime)
            except Exception:
                if self.isDone:
                    return
                raise

            # Publish frame
            self.pvaServer.update(self.channelName, frame)
            self.lastPublishedTime = time.time()
            self.nPublishedFrames += 1

            runtime = 0
            frameRate = 0
            if self.nPublishedFrames > 1:
                runtime = self.lastPublishedTime - self.startTime
                deltaT = runtime/(self.nPublishedFrames - 1)
                frameRate = 1.0/deltaT
            else:
                self.startTime = self.lastPublishedTime
            if self.reportPeriod > 0 and (self.nPublishedFrames % self.reportPeriod) == 0:
                report = 'Published frame id {:6d} @ {:.3f}s (pub rate: {:.4f}fps; pub time: {:.3f}s)'.format(self.currentFrameId, self.lastPublishedTime, frameRate, runtime)
                self.printReport(report)

            if runtime > self.runtime:
                self.printReport(f'Server exiting after reaching runtime of {runtime:.3f} seconds')
                return

            if self.deltaT > 0:
                nextPublishTime = self.startTime + self.nPublishedFrames*self.deltaT
                delay = nextPublishTime - time.time() - self.DELAY_CORRECTION
                if delay > 0:
                    threading.Timer(delay, self.framePublisher).start()
                    return

    def printReport(self, report):
        with self.lock:
            if not self.screenInitialized:
                self.screenInitialized = True
                self.screen = self.setupCurses()
            if self.screen:
                self.screen.erase()
                self.screen.addstr(f'{report}\n')
                self.screen.refresh()
            else:
                print(report)

    def start(self):
        threading.Thread(target=self.frameProducer, daemon=True).start()
        self.pvaServer.start()
        threading.Timer(self.startDelay, self.framePublisher).start()

    def stop(self):
        self.isDone = True
        self.pvaServer.stop()
        runtime = self.lastPublishedTime - self.startTime
        deltaT = 0
        frameRate = 0
        if self.nPublishedFrames > 1:
            deltaT = runtime/(self.nPublishedFrames - 1)
            frameRate = 1.0/deltaT
        dataRate = FloatWithUnits(self.imageSize*frameRate/self.BYTES_IN_MEGABYTE, 'MBps')
        time.sleep(self.SHUTDOWN_DELAY)
        if self.screen:
            self.curses.endwin()
        print('\nServer runtime: {:.4f} seconds'.format(runtime))
        print('Published frames: {:6d} @ {:.4f} fps'.format(self.nPublishedFrames, frameRate))
        print(f'Data rate: {dataRate}')

def main():
    parser = argparse.ArgumentParser(description='PvaPy Area Detector Simulator')
    parser.add_argument('-v', '--version', action='version', version='%(prog)s {version}'.format(version=__version__))
    parser.add_argument('-id', '--input-directory', type=str, dest='input_directory', default=None, help='Directory containing input files to be streamed; if input directory or input file are not provided, random images will be generated')
    parser.add_argument('-if', '--input-file', type=str, dest='input_file', default=None, help='Input file to be streamed; if input directory or input file are not provided, random images will be generated')
    parser.add_argument('-mm', '--mmap-mode', action='store_true', dest='mmap_mode', default=False, help='Use NumPy memory map to load the specified input file. This flag typically results in faster startup and lower memory usage for large files.')
    parser.add_argument('-hds', '--hdf-dataset', dest='hdf_dataset', default=None, help='HDF5 dataset path. This option must be specified if HDF5 files are used as input, but otherwise it is ignored.')
    parser.add_argument('-fps', '--frame-rate', type=float, dest='frame_rate', default=20, help='Frames per second (default: 20 fps)')
    parser.add_argument('-nx', '--n-x-pixels', type=int, dest='n_x_pixels', default=256, help='Number of pixels in x dimension (default: 256 pixels; does not apply if input file file is given)')
    parser.add_argument('-ny', '--n-y-pixels', type=int, dest='n_y_pixels', default=256, help='Number of pixels in x dimension (default: 256 pixels; does not apply if input file is given)')
    parser.add_argument('-dt', '--datatype', type=str, dest='datatype', default='uint8', help='Generated datatype. Possible options are int8, uint8, int16, uint16, int32, uint32, float32, float64 (default: uint8; does not apply if input file is given)')
    parser.add_argument('-mn', '--minimum', type=float, dest='minimum', default=None, help='Minimum generated value (does not apply if input file is given)')
    parser.add_argument('-mx', '--maximum', type=float, dest='maximum', default=None, help='Maximum generated value (does not apply if input file is given)')
    parser.add_argument('-nf', '--n-frames', type=int, dest='n_frames', default=0, help='Number of different frames generate from the input sources; if set to <= 0, the server will use all images found in input files, or it will generate enough images to fill up the image cache if no input files were specified.')
    parser.add_argument('-cs', '--cache-size', type=int, dest='cache_size', default=1000, help='Number of different frames to cache (default: 1000); if the cache size is smaller than the number of input frames, the new frames will be constantly regenerated as cached ones are published; otherwise, cached frames will be published over and over again as long as the server is running')
    parser.add_argument('-rt', '--runtime', type=float, dest='runtime', default=300, help='Server runtime in seconds (default: 300 seconds)')
    parser.add_argument('-cn', '--channel-name', type=str, dest='channel_name', default='pvapy:image', help='Server PVA channel name (default: pvapy:image)')
    parser.add_argument('-npv', '--notify-pv', type=str, dest='notify_pv', default=None, help='CA channel that should be notified on start; for the default Area Detector PVA driver PV that controls image acquisition is 13PVA1:cam1:Acquire')
    parser.add_argument('-nvl', '--notify-pv-value', type=str, dest='notify_pv_value', default='1', help='Value for the notification channel; for the Area Detector PVA driver PV this should be set to "Acquire" (default: 1)')
    parser.add_argument('-mpv', '--metadata-pv', type=str, dest='metadata_pv', default=None, help='Comma-separated list of CA channels that should be contain simulated image metadata values')
    parser.add_argument('-sd', '--start-delay', type=float, dest='start_delay',  default=10.0, help='Server start delay in seconds (default: 10 seconds)')
    parser.add_argument('-rp', '--report-period', type=int, dest='report_period', default=1, help='Reporting period for publishing frames; if set to <=0 no frames will be reported as published (default: 1)')
    parser.add_argument('-dc', '--disable-curses', dest='disable_curses', default=False, action='store_true', help='Disable curses library screen handling. This is enabled by default, except when logging into standard output is turned on.')

    args, unparsed = parser.parse_known_args()
    if len(unparsed) > 0:
        print('Unrecognized argument(s): %s' % ' '.join(unparsed))
        exit(1)

    server = AdSimServer(inputDirectory=args.input_directory, inputFile=args.input_file, mmapMode=args.mmap_mode, hdfDataset=args.hdf_dataset, frameRate=args.frame_rate, nFrames=args.n_frames, cacheSize=args.cache_size, nx=args.n_x_pixels, ny=args.n_y_pixels, datatype=args.datatype, minimum=args.minimum, maximum=args.maximum, runtime=args.runtime, channelName=args.channel_name, notifyPv=args.notify_pv, notifyPvValue=args.notify_pv_value, metadataPv=args.metadata_pv, startDelay=args.start_delay, reportPeriod=args.report_period, disableCurses=args.disable_curses)

    server.start()
    try:
        waitTime = args.runtime+args.start_delay+server.SHUTDOWN_DELAY
        time.sleep(waitTime)
    except KeyboardInterrupt as ex:
        pass
    server.stop()

if __name__ == '__main__':
    main()
