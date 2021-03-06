#!/usr/bin/env python
################################################################################
#    Created by Oscar Martinez                                                 #
#    o.rubi@esciencecenter.nl                                                  #
################################################################################
import os,logging
from pointcloud.oracle.AbstractLoader import AbstractLoader
from pointcloud import lasops

class LoaderExt(AbstractLoader):
    def initialize(self):
        # Check parameters for this loader
        if self.columns != 'xyz':
            raise Exception('ERROR: This loader only currently accepts XYZ!. First you need to change the hilbert pre-processor')
        
        if self.cUser:
            self.createUser()
        
        # Get the point cloud folder description
        logging.info('Getting files, extent and SRID from input folder ' + self.inputFolder)
        (self.inputFiles, _, _, _, boundingCube, _) = lasops.getPCFolderDetails(self.inputFolder, numProc = self.numProcessesLoad)
        (self.minX, self.minY, _, self.maxX, self.maxY, _) = boundingCube
        
        # Get the parent folder and the wildcard text with file selection
        if os.path.isfile(self.inputFolder):
            parentFolder = os.path.abspath(os.path.join(self.inputFolder,'..'))
            lasFiles = os.path.basename(self.inputFolder)
            extension = self.inputFolder.split('.')[-1]
        else:
            parentFolder = self.inputFolder
            if len(self.inputFiles) == 0:
                raise Exception('ERROR: None PC file in ' + self.inputFolder)
            extension = self.inputFiles[0].split('.')[-1]
            lasFiles = '*.' + extension
        if extension.lower() == 'laz':
            raise Exception('ERROR: pre-processor only accepts LAS files!')            

        # We define the External table name
        self.flatTable = self.blockTable + '_STAGING'
        self.extTable = ('EXT_' + self.flatTable).upper()
        self.lasDirVariableName = (self.userName + '_las_dir').upper()
        
        # Create the Oracle directory within the DB
        self.createLASDirectory(self.lasDirVariableName, parentFolder)

        connection = self.getConnection()
        cursor = connection.cursor()
        
        self.colsToUse = list(self.columns)
        # If hilbert method is required we need to request also the hilbert code from the LAS reader
        if self.blockMethod == 'hilbert':
            if 'h' not in self.colsToUse:
                self.colsToUse.append('h')
        
        # Define the external table (which use the preprocessor file in Oracle directory EXE_DIR)
        self.createExternalTable(cursor, lasFiles, self.extTable, self.colsToUse, self.lasDirVariableName, self.numProcessesLoad)
        # Create the blocks table
        self.createBlocksTable(cursor, self.blockTable, self.tableSpace, self.compression, self.baseTable)
        connection.close()

    def process(self):
        logging.info('Starting data loading in parallel by an external table loader from ' + self.inputFolder + ' to ' + self.userName)
        return self.processSingle([self.inputFolder,], self.loadInputFolder)
    
    def loadInputFolder(self, index, inputFolder):
        if self.blockMethod == 'hilbert':
            # In the case of Hilbert we create the IOT with the data (we do not do it for regular rtree since points sorting is part of the rtree blocking procedure)
            connection = self.getConnection()
            cursor = connection.cursor()

            # Create the IOT (note that we store it in the index table space!)
            self.createIOTTable(cursor, self.flatTable, self.extTable, self.indexTableSpace, self.colsToUse, self.colsToUse, ['h',], self.numProcessesLoad, False, self.hilbertFactor)

            connection.close()

    def close(self):
        connection = self.getConnection()
        cursor = connection.cursor()
        
        if self.blockMethod == 'hilbert':
            # Create blocks and index
            self.populateBlocksHilbert(cursor, self.srid, self.minX, self.minY, self.maxX, self.maxY, self.flatTable, self.blockTable, self.baseTable, self.blockSize, self.tolerance)
            self.createBlockIndex(cursor, self.srid, self.minX, self.minY, self.maxX, self.maxY, self.blockTable, self.indexTableSpace, self.workTableSpace, self.numProcessesLoad)
        else: #Rtree blocking
            self.populateBlocks(cursor, self.srid, self.minX, self.minY, self.maxX, self.maxY, self.extTable, self.blockTable, self.baseTable, self.blockSize, self.columns, self.tolerance, self.workTableSpace)
            self.createBlockIdIndex(cursor, self.blockTable, self.indexTableSpace)
        self.computeStatistics(cursor, self.blockTable) 
        connection.close()
        
    def size(self):
        return self.sizeBlocks(self.blockTable, self.baseTable)
        
    def getNumPoints(self):
        return self.getNumPointsBlocks(self.blockTable)
