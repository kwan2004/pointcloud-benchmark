#!/usr/bin/env python
################################################################################
#    Created by Oscar Martinez                                                 #
#    o.rubi@esciencecenter.nl                                                  #
################################################################################
import time, copy, logging
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

        # Create table to store the query geometries
        oracleops.dropTable(cursor, self.queryTable, check = True)
        oracleops.mogrifyExecute(cursor, "CREATE TABLE " + self.queryTable + " ( id number primary key, geom sdo_geometry) " + self.getTableSpaceString(self.tableSpace) + " pctfree 0 nologging")
        connection.close()
                
        self.colsDict = self.getColumnNamesDict(False)

    def query(self, queryId, iterationId, queriesParameters):
        (eTime, result) = (-1, None)
        connection = self.getConnection()
        cursor = connection.cursor()
    
        self.prepareQuery(cursor, queryId, queriesParameters,  False)#iterationId == 0,
        oracleops.dropTable(cursor, self.resultTable, True)

        range_table_name = "range_" + queryId

        oracleops.dropTable(cursor, range_table_name, True)
#        rangetable_sql =  "CREATE TABLE " + range_table_name + " (K1 NUMBER PRIMARY KEY, K2 NUMBER) ORGANIZATION INDEX "
        rangetable_sql =  "CREATE TABLE " + range_table_name + " (K1 NUMBER, K2 NUMBER, CONSTRAINT pk_index PRIMARY KEY (K1)) ORGANIZATION INDEX "
        cursor.execute(rangetable_sql)
        logging.info(rangetable_sql)

        if self.numProcessesQuery > 1 and self.parallelType != 'nati': 
            if self.qp.queryType in ('rectangle','circle','generic') :
                 return self.pythonParallelization()
            else:
                 logging.error('Python parallelization only available for queries which are not NN!')
                 return (eTime, result)
                         
        t0 = time.time()
        connstring = self.getConnectionString()

#        (query, _) = dbops.getSelect(self.qp, self.flatTable, self.addContainsCondition, self.colsDict, self.getParallelHint())
        (query, _) = dbops.getSelect2(self.qp, queryId, self.flatTable, self.addContainsCondition, self.colsDict, range_table_name, connstring, self.getParallelHint() )

        if self.qp.queryMethod != 'stream': # disk or stat
            oracleops.mogrifyExecute(cursor, "CREATE TABLE "  + self.resultTable + " AS " + query)
            (eTime, result) = dbops.getResult(cursor, t0, self.resultTable, self.colsDict, True, self.qp.columns, self.qp.statistics)
        else:
            #sqlFileName = str(queryId) + '.sql'
            #oracleops.createSQLFile(cursor, sqlFileName, query, None)
            #result = oracleops.executeSQLFileCount(self.getConnectionString(False), sqlFileName)
            #eTime = time.time() - t0

            oracleops.mogrifyExecute( cursor, "CREATE TABLE " + self.resultTable + " AS " + query )
            (eTime, result) = dbops.getResult( cursor, t0, self.resultTable, self.colsDict, True, self.qp.columns,
                                               self.qp.statistics )

            # second refinement
            tblname = self.resultTable + "_01"
            zname = self.colsDict['z'][0]
            lname = self.colsDict['l'][0]

            #4D----xyzl
            if self.qp.minz != -99999999 and self.qp.minl != -99999999:
                querya = """CREATE  TABLE """ + tblname + """ AS (SELECT /* + PARALLEL( 8 ) */ * FROM TABLE( mdsys.sdo_PointInPolygon( CURSOR( SELECT * FROM """ + self.resultTable + """ ),
                    MDSYS.SDO_GEOMETRY( '""" + self.qp.wkt + """', 28992), 0.001)) WHERE ( """ + str(self.qp.minz) + """ <= """ + \
                        zname + """ AND """ + zname + """ <= """ + str(self.qp.maxz) + """ AND """ + str(self.qp.minl) + """ <= """ + \
                        lname + """ AND """ + lname + """ <= """ + str(self.qp.maxl) + """ ))"""
            #3D---only l filter
            if self.qp.minz == -99999999 and self.qp.minl != -99999999:
                querya = """CREATE  TABLE """ + tblname + """ AS (SELECT /* + PARALLEL( 8 ) */ * FROM TABLE( mdsys.sdo_PointInPolygon( CURSOR( SELECT * FROM """ + self.resultTable + """ ),
                    MDSYS.SDO_GEOMETRY( '""" + self.qp.wkt + """', 28992), 0.001)) WHERE ( """ + str(self.qp.minl) + """ <= """ + \
                        lname + """ AND """ + lname + """ <= """ + str(self.qp.maxl) + """ ))"""
            #3D---only z filter
            if self.qp.minz != -99999999 and self.qp.minl == -99999999:
                querya = """CREATE  TABLE """ + tblname + """ AS (SELECT /* + PARALLEL( 8 ) */ * FROM TABLE( mdsys.sdo_PointInPolygon( CURSOR( SELECT * FROM """ + self.resultTable + """ ),
                    MDSYS.SDO_GEOMETRY( '""" + self.qp.wkt + """', 28992), 0.001)) WHERE ( """ + str(self.qp.minz) + """ <= """ + \
                        zname + """ AND """ + zname + """ <= """ + str(self.qp.maxz) + """ ))"""
            #2D---xy
            if self.qp.minz == -99999999 and self.qp.minl == -99999999:
                querya = """CREATE  TABLE """ + tblname + """ AS (SELECT /* + PARALLEL( 8 ) */ * FROM TABLE( mdsys.sdo_PointInPolygon( CURSOR( SELECT * FROM """ + self.resultTable + """ ),
                    MDSYS.SDO_GEOMETRY( '""" + self.qp.wkt + """', 28992), 0.001)) )"""


            oracleops.mogrifyExecute( cursor, querya)
            (eTime, result) = dbops.getResult( cursor, t0, self.resultTable, self.colsDict, True, self.qp.columns,
                                               self.qp.statistics )
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
        (eTime, result) =  dbops.genericQueryParallelGrid(cursor, self.qp.queryMethod, oracleops.mogrifyExecute, self.qp.columns, self.colsDict,
         self.qp.statistics, self.resultTable, gridTable, self.createGridTableMethod,
         self.runGenericQueryParallelGridChild, self.numProcessesQuery,
         (self.parallelType == 'griddis'), oracleops.createSQLFile, oracleops.executeSQLFileCount, self.getConnectionString(False))
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
        (query, _) = dbops.getSelect(cqp, self.flatTable, self.addContainsCondition, self.colsDict)
        oracleops.mogrifyExecute(cursor, """
        DECLARE
          bbox sdo_geometry;
        BEGIN
          select sdo_geom_mbr (geom) into bbox from """ + gridTable + """ where id = """ + str(sIndex) + """;
          execute immediate 'INSERT INTO """ + self.resultTable + """ """ + query + """';
        END;""")

        connection.close()
