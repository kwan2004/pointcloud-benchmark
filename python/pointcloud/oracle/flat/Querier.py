#!/usr/bin/env python
################################################################################
#    Created by Oscar Martinez                                                 #
#    o.rubi@esciencecenter.nl                                                  #
################################################################################
import time, copy
from pointcloud import dbops, oracleops
from pointcloud.oracle.AbstractQuerier import AbstractQuerier

class Querier(AbstractQuerier):
    def initialize(self):
        # Get connection
        connection = self.getConnection()
        cursor = connection.cursor()
        # Get SRID of the stored PC
        oracleops.mogrifyExecute(cursor, "SELECT srid, minx, miny, maxx, maxy, scalex, scaley from " + self.metaTable)
        (self.srid, minX, minY, maxX, maxY, scaleX, scaleY) = cursor.fetchone()
                
    def query(self, queryId, iterationId, queriesParameters):
        (eTime, result) = (-1, None)
        connection = self.getConnection()
        cursor = connection.cursor()
    
        self.prepareQuery(cursor, queryId, queriesParameters, iterationId == 0)
        oracleops.dropTable(cursor, self.resultTable, True) 
        
        if self.qp.queryMethod != 'stream' and self.numProcessesQuery > 1 and self.parallelType != 'nati' and self.qp.queryType in ('rectangle','circle','generic') :
             return self.pythonParallelization()
        
        t0 = time.time()
        (query, _) = dbops.getSelect(self.qp, self.flatTable, self.addContainsCondition, self.colsData, self.getParallelHint())
        
        if self.qp.queryMethod != 'stream': # disk or stat
            oracleops.mogrifyExecute(cursor, "CREATE TABLE "  + self.resultTable + " AS " + query)
            (eTime, result) = dbops.getResult(cursor, t0, self.resultTable, self.colsData, True, self.qp.columns, self.qp.statistics)
        else:
            sqlFileName = str(queryId) + '.sql'
            oracleops.createSQLFile(cursor, sqlFileName, query, None)
            result = oracleops.executeSQLFileCount(self.getConnectionString(False), sqlFileName)
            eTime = time.time() - t0
        connection.close()
        return (eTime, result)

    #
    # METHOD RELATED TO THE QUERIES OUT-OF-CORE PYTHON PARALLELIZATION 
    #
    def pythonParallelization(self):
        connection = self.getConnection()
        cursor = connection.cursor()
        gridTable = ('query_grid_' + str(self.queryIndex)).upper()
        oracleops.dropTable(cursor, gridTable, True)
        (eTime, result) =  dbops.genericQueryParallelGrid(cursor, oracleops.mogrifyExecute, self.qp.columns, self.colsData, 
             self.qp.statistics, self.resultTable, gridTable, self.createGridTableMethod,
             self.runGenericQueryParallelGridChild, self.numProcessesQuery, 
             (self.parallelType == 'griddis'))
        connection.close()
        return (eTime, result)

    def runGenericQueryParallelGridChild(self, sIndex, gridTable):
        connection = self.getConnection()
        cursor = connection.cursor()
        self.queryIndex = sIndex
        self.queryTable = gridTable
        cqp = copy.copy(self.qp)
        cqp.queryType = 'generic'
        if self.qp.queryType == 'rectangle':
            cqp.queryType = 'rectangle'
        cqp.statistics = None
        cqp.minx = "'||to_char(bbox.sdo_ordinates(1))||'"
        cqp.maxx = "'||to_char(bbox.sdo_ordinates(3))||'" 
        cqp.miny = "'||to_char(bbox.sdo_ordinates(2))||'"
        cqp.maxy = "'||to_char(bbox.sdo_ordinates(4))||'" 
        (query, _) = dbops.getSelect(cqp, self.flatTable, self.addContainsCondition, self.colsData)
        oracleops.mogrifyExecute(cursor, """
DECLARE
  bbox sdo_geometry;
BEGIN
  select sdo_geom_mbr (geom) into bbox from """ + gridTable + """ where id = """ + str(sIndex) + """;
  execute immediate 'INSERT INTO """ + self.resultTable + """ """ + query + """';
END;""")
        connection.close()
