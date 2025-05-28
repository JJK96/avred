import logging
import time
import math
from intervaltree import Interval, IntervalTree
from typing import List
from copy import deepcopy
from dataclasses import dataclass

from model.model_base import Scanner, ScanSpeed
from model.model_data import Data, Match
from model.file_model import BaseFile

from myutils import *

PRINT_DELAY_SECONDS = 2


@dataclass
class Part():
    data:bytes
    start:int
    end:int
    detected:bool = None


class Reducer():
    """Reducer will scan data in file with scanner, and return List of matches"""

    def __init__(self, file: BaseFile, scanner: Scanner, iteration: int = 0, scanSpeed: ScanSpeed = ScanSpeed.Normal, matchIdx: int = 0):
        self.file: BaseFile = file
        self.scanner: Scanner = scanner
        self.scanSpeed: ScanSpeed = scanSpeed
        self.iteration: int = iteration
        self.matchIdx: int = matchIdx

        self.matchesAdded: int = 0
        self.chunks_tested: int = 0

        self.minMatchSize: int = 4
        self.minChunkSize: int = 4  # sane default for now. Will be adjusted based on section size on scan()

        # re-init for every scan
        self.lastPrintTime: int = 0
        self.it = IntervalTree()


    def init(self):
        self.it = IntervalTree()
        self.lastPrintTime = 0


    def scan(self, offsetStart, offsetEnd) -> List[Match]:
        """Scan self.file.Data() from offsetStart to offsetEnd, return matches"""
        self.init()
        data = deepcopy(self.file.Data())  # get the data of the file to work on as copy

        size = offsetEnd - offsetStart
        if size < 50000: # 50kb
            self.minChunkSize = 2
        elif size < 100000: # 100kb
            self.minChunkSize = 8
        elif size < 500000: # 500kb
            self.minChunkSize = 16
        elif size < 1000000: # 1mb
            self.minChunkSize = 32
        else: # >1mb
            self.minChunkSize = 64
        self.minMatchSize = self.minChunkSize * 2

        logging.info("Reducer Start: ScanSpeed:{} Iteration:{} MinChunkSize:{} MinMatchSize:{}".format(
            self.scanSpeed.name, self.iteration, self.minChunkSize, self.minMatchSize))
        timeStart = time.time()
        self._scanDataPart(data, offsetStart, offsetEnd)
        timeEnd = time.time()

        scanTime = round(timeEnd - timeStart)
        logging.info("Reducer Result: Time:{} Chunks:{} MatchesAdded:{} MatchesFinal:{}".format(
            scanTime, self.chunks_tested, self.matchesAdded, len(self.it)))
        matches = convertMatchesIt(self.it, self.iteration, self.matchIdx)
        self.matchIdx += len(matches)

        return matches


    def _scanData(self, data: Data):
        """Use self.file with data, scan it and return true/false"""
        newFileData: Data = self.file.getFileDataWith(data)
        return self.scanner.scannerDetectsBytes(newFileData.getBytes(), self.file.filename)


    def _addMatch(self, sectionStart: int, sectionEnd: int):
        self.it.add ( Interval(sectionStart, sectionEnd) )
        self.matchesAdded += 1

        # Always merge, so we have accurate information about the amount of real matches
        self.it.merge_overlaps(strict=False)


    # recursive
    def _scanDataPart(self, data: Data, sectionStart: int, sectionEnd: int, num_chunks=2):
        size = sectionEnd - sectionStart
        chunkSize = math.ceil(size / num_chunks)
        self.chunks_tested += 1
        self._printStatus()

        #logging.info(f"Testing: {sectionStart}-{sectionEnd} with size {sectionEnd-sectionStart} (chunkSize {chunkSize} bytes)")
        #logging.info(f"Testing Top: {sectionStart}-{sectionStart+chunkSize}")
        #logging.info(f"Testing Bot: {sectionStart+chunkSize}-{sectionStart+chunkSize+chunkSize}")

        if self.chunks_tested > 0 and self.chunks_tested % 100 == 0:
            logging.info("Doubling: minChunkSize: {}  minMatchSize: {}".format(
                self.minChunkSize, self.minMatchSize
            ))
            self.minChunkSize *= 2
            self.minMatchSize *= 2

        # dangling bytes
        # note that these have been detected, thats why they are being scanned.
        # so we can just add them
        if chunkSize <= self.minChunkSize:
            dataBytes = data.getBytesRange(sectionStart, sectionEnd)
            logging.info(f"Result: {sectionStart}-{sectionEnd} ({sectionEnd-sectionStart}b minChunk:{self.minChunkSize} X)"
                            + "\n" + hexdmp(dataBytes, offset=sectionStart))
            self._addMatch(sectionStart, sectionEnd)
            
            return

        parts = []
        startOffset = sectionStart
        for i in range(num_chunks):
            datachunk = deepcopy(data)
            datachunk.patchDataFill(startOffset, chunkSize)
            endOffset = startOffset+chunkSize
            if i == num_chunks - 1:
                endOffset = sectionEnd
            part = Part(datachunk, startOffset, endOffset)
            part.detected = self._scanData(part.data)
            parts.append(part)
            startOffset = endOffset

        if all(part.detected for part in parts):
            # All parts are detected
            # Continue scanning both halves independantly, but with the rest of the data
            # zeroed out (instead of the complete file)
            for part in parts:
                datachunk = deepcopy(data)
                if part.start != sectionStart:
                    datachunk.patchDataFill(sectionStart, part.start - sectionStart)
                if part.end != sectionEnd:
                    datachunk.patchDataFill(part.end, sectionEnd - part.end)
                self._scanDataPart(datachunk, part.start, part.end)

        elif not any(part.detected for part in parts):
            # all parts arent detected anymore

            if chunkSize <= self.minMatchSize:
                # Small enough, no more detections.
                # The "previous" section is our match
                dataBytes = data.getBytesRange(sectionStart, sectionStart+size)
                logging.info(f"Result: {sectionStart}-{sectionEnd} ({sectionEnd-sectionStart} bytes)" 
                             + "\n" + hexdmp(dataBytes, offset=sectionStart))
                self._addMatch(sectionStart, sectionStart+size)
            else: 
                # make it smaller still. 
                # Take complete data (not nulled)
                for part in parts:
                    self._scanDataPart(data, part.start, part.end)

        else:
            for part in parts:
                if not part.detected:
                    self._scanDataPart(data, part.start, part.end)

        return


    def _printStatus(self):
        currentTime = time.time()
        if currentTime > self.lastPrintTime + PRINT_DELAY_SECONDS:
            self.lastPrintTime = currentTime
            logging.info("Reducing: {} chunks done, found {} matches ({} added)".format(
                self.chunks_tested, len(self.it), self.matchesAdded))


def convertMatchesIt(matchesIt: IntervalTree, iteration: int = 0, baseIdx: int = 0) -> List[Match]:
    matches: List[Match] = []
    idx = 0 + baseIdx
    for m in sorted(matchesIt):
        match = Match(idx, m.begin, m.end-m.begin, iteration)
        matches.append(match)
        idx += 1
    return matches
