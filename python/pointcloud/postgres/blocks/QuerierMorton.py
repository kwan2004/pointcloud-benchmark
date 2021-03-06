#!/usr/bin/env python
################################################################################
#    Created by Oscar Martinez                                                 #
#    o.rubi@esciencecenter.nl                                                  #
################################################################################
from shapely.wkt import loads, dumps
import time,copy,logging
from pointcloud.postgres.AbstractQuerier import AbstractQuerier
from pointcloud import wktops, dbops, postgresops
from pointcloud.QuadTree import QuadTree

MAXIMUM_RANGES = 10

class QuerierMorton(AbstractQuerier):        
    def initialize(self):
        connection = self.getConnection()
        cursor = connection.cursor()
        
        self.metaTable = self.blockTable + '_meta'
        postgresops.mogrifyExecute(cursor, "SELECT srid, minx, miny, maxx, maxy, scalex, scaley from " + self.metaTable)
        (self.srid, self.minX, self.minY, self.maxX, self.maxY, self.scaleX, self.scaleY) = cursor.fetchone()
        
        postgresops.dropTable(cursor, self.queryTable, check = True)
        postgresops.mogrifyExecute(cursor, "CREATE TABLE " +  self.queryTable + " (id integer, geom public.geometry(Geometry," + str(self.srid) + "));")
        
        connection.close()
        
        self.columnsNameDict = {}
        for c in self.DM_PDAL:
            if self.DM_PDAL[c] != None:
                self.columnsNameDict[c] = ("PC_Get(qpoint, '" + self.DM_PDAL[c].lower() + "')",)
        
        qtDomain = (0, 0, int((self.maxX-self.minX)/self.scaleX), int((self.maxY-self.minY)/self.scaleY))
        self.quadtree = QuadTree(qtDomain, 'auto')    
        # Differentiate QuadTree nodes that are fully in the query region
        self.mortonDistinctIn = False
        
    def query(self, queryId, iterationId, queriesParameters):
        (eTime, result) = (-1, None)
        connection = self.getConnection()
        cursor = connection.cursor()
               
        self.prepareQuery(cursor, queryId, queriesParameters, iterationId == 0)
        postgresops.dropTable(cursor, self.resultTable, True)    
       
        wkt = self.qp.wkt
        if self.qp.queryType == 'nn':
            g = loads(self.qp.wkt)
            wkt = dumps(g.buffer(self.qp.rad))
       
        t0 = time.time()
        scaledWKT = wktops.scale(wkt, self.scaleX, self.scaleY, self.minX, self.minY)    
        (mimranges,mxmranges) = self.quadtree.getMortonRanges(scaledWKT, self.mortonDistinctIn, maxRanges = MAXIMUM_RANGES)
       
        if len(mimranges) == 0 and len(mxmranges) == 0:
            logging.info('None morton range in specified extent!')
            return (eTime, result)

        if self.qp.queryType == 'nn':
            logging.error('NN queries not available!')
            return (eTime, result)

        if self.numProcessesQuery > 1:
            if self.qp.queryMethod != 'stream' and self.qp.queryType in ('rectangle','circle','generic') :
                 return self.pythonParallelization(t0, mimranges, mxmranges)
            else:
                 logging.error('Python parallelization only available for disk queries (CTAS) which are not NN queries!')
                 return (eTime, result)
        
        (query, queryArgs) = self.getSelect(self.qp, mimranges, mxmranges)        
         
        if self.qp.queryMethod != 'stream': # disk or stat
            postgresops.mogrifyExecute(cursor, "CREATE TABLE "  + self.resultTable + " AS (" + query + ")", queryArgs)
            (eTime, result) = dbops.getResult(cursor, t0, self.resultTable, self.DM_FLAT, (not self.mortonDistinctIn), self.qp.columns, self.qp.statistics)
        else:
            sqlFileName = str(queryId) + '.sql'
            postgresops.createSQLFile(cursor, sqlFileName, query, queryArgs)
            result = postgresops.executeSQLFileCount(self.getConnectionString(False, True), sqlFileName)
            eTime = time.time() - t0
            
        connection.close()
        return (eTime, result)
 
    def addContains(self, queryArgs):
        queryArgs.append(self.queryIndex)
        return "pc_intersects(pa,geom) and " + self.queryTable + ".id = %s"
    
    def getSelect(self, qp, iMortonRanges, xMortonRanges):
        queryArgs = []
        query = ''
        
        zname = self.columnsNameDict['z'][0]
        kname = 'quadcellid'
        
        if len(iMortonRanges):
            if qp.queryType == 'nn':
                raise Exception('If using NN len(iMortonRanges) must be 0!')
            cols = dbops.getSelectCols(qp.columns, self.columnsNameDict, None, True)
            inMortonCondition = dbops.addMortonCondition(qp, iMortonRanges, kname, queryArgs) 
            inZCondition = dbops.addZCondition(qp, zname, queryArgs)
            query = "SELECT " + cols + " FROM (SELECT PC_Explode(pa) as qpoint from " + self.blockTable + dbops.getWhereStatement(inMortonCondition) + ") as qtable1 " + dbops.getWhereStatement(inZCondition) + " UNION "
        else:
            cols = dbops.getSelectCols(qp.columns, self.columnsNameDict, qp.statistics, True)
        
        mortonCondition = dbops.addMortonCondition(qp, xMortonRanges, kname, queryArgs)
        
        if qp.queryType in ('rectangle', 'circle', 'generic'):
            containsCondition = self.addContains(queryArgs)
            zCondition = dbops.addZCondition(qp, zname, queryArgs)
            query += "SELECT " + cols + " FROM (SELECT PC_Explode(PC_Intersection(pa,geom)) as qpoint from " + self.blockTable + ', ' + self.queryTable + dbops.getWhereStatement([mortonCondition, containsCondition]) + ") as qtable2 " + dbops.getWhereStatement(zCondition)
        elif qp.queryType != 'nn':
            #Approximation
            query += "SELECT " + cols + " FROM (SELECT PC_Explode(pa) as qpoint from " + self.blockTable + dbops.getWhereStatement(mortonCondition) + ") as qtable3 "
        return (query, queryArgs)
   
    #
    # METHOD RELATED TO THE QUERIES OUT-OF-CORE PYTHON PARALLELIZATION 
    #
    def pythonParallelization(self, t0, mimranges, mxmranges):
        connection = self.getConnection()
        cursor = connection.cursor()
        dbops.createResultsTable(cursor, postgresops.mogrifyExecute, self.resultTable, self.qp.columns, self.DM_FLAT)
        dbops.parallelMorton(mimranges, mxmranges, self.childInsert, self.numProcessesQuery)
        (eTime, result) = dbops.getResult(cursor, t0, self.resultTable, self.DM_FLAT, False, self.qp.columns, self.qp.statistics)
        connection.close()
        return (eTime, result)
    
    def childInsert(self, iMortonRanges, xMortonRanges):
        connection = self.getConnection()
        cqp = copy.copy(self.qp)
        cqp.statistics = None
        (query, queryArgs) = self.getSelect(cqp, iMortonRanges, xMortonRanges)
        postgresops.mogrifyExecute(connection.cursor(), "INSERT INTO " + self.resultTable + " "  + query, queryArgs)
        connection.close()  
