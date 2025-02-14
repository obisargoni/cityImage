import warnings
warnings.simplefilter(action='ignore')

import pandas as pd
import numpy as np
import geopandas as gpd
from shapely.geometry import Point, LineString, Polygon, MultiPoint
from shapely.ops import linemerge, nearest_points, split, polygonize_full, unary_union

pd.set_option("display.precision", 3)
pd.options.mode.chained_assignment = None

from .graph import nodes_degree
from .utilities import center_line, center_line_coords, merge_lines, split_line_at_interpolation
from .clean import clean_network, correct_edges
from .angles import difference_angle_line_geometries, angle_line_geometries, is_parallel, is_continuation
from .simplify_junctions import simplify_complex_junctions, simplify_dual_lines_junctions, dissolve_roundabouts, interpolate_on_center_line
                    

def identify_clusters(nodes_gdf, edges_gdf, radius = 10):   
    
    """
    This function simplifies complex junctions as trinagular-like junctions formed mainly by secondary links.
    The junction may be as well represented by one node rather than, for example three nodes. 
               
    The function takes a node and check whether the intersecting edges give shape to a triangular-cyclic junction.
    
    A new dataframe with the simplified geometries is returned.
    
    Parameters
    ----------
    nodes_gdf: Point GeoDataFrame
    edges_gdf: LineString GeoDataFrames
   
    Returns
    -------
    GeoDataFrames
    """   
    
    nodes_gdf.index, edges_gdf.index = nodes_gdf.nodeID, edges_gdf.edgeID
    nodes_gdf.index.name, edges_gdf.index.name = None, None
    nodes_gdf, edges_gdf = nodes_gdf.copy(), edges_gdf.copy()
  
    to_ignore = {k: v for k, v in nodes_degree(edges_gdf).items() if v == 1}
    tmp_nodes_gdf = nodes_gdf[~nodes_gdf.nodeID.isin(list(to_ignore.keys()))].copy() #ignoring dead-ends
    buffered_nodes = tmp_nodes_gdf.buffer(radius).unary_union
    if isinstance(buffered_nodes, Polygon): 
        buffered_nodes = [buffered_nodes]
        
    buffered_nodes_geoS = gpd.GeoSeries(list(buffered_nodes))
    buffered_nodes_df =  pd.concat([buffered_nodes_geoS.rename('geometry'), pd.Series(buffered_nodes_geoS.index).rename('clusterID')], axis=1)

    buffered_nodes_gdf = gpd.GeoDataFrame(buffered_nodes_df, geometry = buffered_nodes_df.geometry)
    buffered_nodes_gdf['area']= buffered_nodes_gdf['geometry'].area
    buffered_nodes_gdf['centroid'] = buffered_nodes_gdf.geometry.centroid
    
    clusters_gdf = buffered_nodes_gdf[buffered_nodes_gdf["area"] > (radius*radius*3.14159)]
    clusters_gdf['x'], clusters_gdf['y'] = (clusters_gdf.geometry.centroid.x, clusters_gdf.geometry.centroid.y)
    clusters_gdf.index += nodes_gdf.index.max()+1
    clusters_gdf['clusterID'] = clusters_gdf.index
    
    # set cluster column values
    nodes_gdf["cluster"] = None
    nodes_gdf["cluster"] = nodes_gdf.apply(lambda row: _assign_cluster_nodes(row["geometry"], clusters_gdf), axis = 1)
    nodes_gdf = nodes_gdf.where(pd.notnull(nodes_gdf), None)
    nodes_gdf.loc[nodes_gdf.nodeID.isin(list(to_ignore.keys())), "cluster"] = None
    
    clusters_counts = dict(nodes_gdf['cluster'].value_counts())
    clusters_gdf['degree'] = 0
    clusters_gdf['degree'] = clusters_gdf['clusterID'].map(clusters_counts)
    
    geometry = clusters_gdf['centroid']
    data = clusters_gdf.drop(['centroid', 'geometry'], axis=1)
    clusters_gdf = gpd.GeoDataFrame(data, crs= nodes_gdf.crs, geometry=geometry)
    edges_gdf = _assign_cluster_edges(nodes_gdf, edges_gdf, clusters_gdf)
    
    return(nodes_gdf, edges_gdf, clusters_gdf)
 
def _assign_cluster_nodes(node_geometry, clusters_gdf): #ok
        
    ix_geo = clusters_gdf.columns.get_loc("geometry")+1
    ix_cluster = clusters_gdf.columns.get_loc("clusterID")+1
    
    tmp = clusters_gdf[clusters_gdf["geometry"].intersects(node_geometry.buffer(1))]
    if len(tmp) == 0: 
        return None
    for cluster in tmp.itertuples():
        if node_geometry.within(cluster[ix_geo]): 
            return int(cluster[ix_cluster])

def _assign_cluster_edges(nodes_gdf, edges_gdf, clusters_gdf):
    
    nodes_gdf.set_index('nodeID', drop = False, append = False, inplace = True)
    nodes_gdf.index.name = None
    
    edges_gdf.drop(['nodeID_x', 'nodeID_y','clus_uR', 'clus_vR', 'clus_u', 'clus_v'], axis = 1, inplace = True, errors = 'ignore')
    edges_gdf = pd.merge(edges_gdf, nodes_gdf[['cluster', 'nodeID']], how = 'left', left_on= "u", right_on = "nodeID")
    edges_gdf = edges_gdf.rename(columns = {'cluster':'clus_u'})
    edges_gdf = pd.merge(edges_gdf, nodes_gdf[['cluster', 'nodeID']], how = 'left', left_on= "v", right_on = "nodeID")
    edges_gdf = edges_gdf.rename(columns = {'cluster':'clus_v'})  
    edges_gdf.set_index('edgeID', drop = False, append = False, inplace = True)
    edges_gdf.index.name = None

    edges_gdf['clus_uR'], edges_gdf['clus_vR'] = None, None
    ix_clus_u, ix_clus_v  = edges_gdf.columns.get_loc("clus_u")+1, edges_gdf.columns.get_loc("clus_v")+1
    ix_clus_uR, ix_clus_vR = edges_gdf.columns.get_loc("clus_uR")+1, edges_gdf.columns.get_loc("clus_vR")+1
   
    # assigning cluster
    tmp = edges_gdf[(edges_gdf['clus_u'].isnull())].copy()
    edges_gdf['clus_uR'] = tmp.apply(lambda row: indirect_cluster(nodes_gdf, edges_gdf, clusters_gdf, row['edgeID'],
                                            'u')[0], axis = 1)
    tmp = edges_gdf[(edges_gdf['clus_v'].isnull())].copy()
    edges_gdf['clus_vR'] = tmp.apply(lambda row: indirect_cluster(nodes_gdf, edges_gdf, clusters_gdf, row['edgeID'],
                                            'v')[0], axis = 1)
    edges_gdf = edges_gdf.where(pd.notnull(edges_gdf), None)
    edges_gdf.drop(['nodeID_x', 'nodeID_y'], axis = 1, inplace = True, errors = 'ignore')       
    return(edges_gdf)
   
def indirect_cluster(nodes_gdf, edges_gdf, clusters_gdf, ix_line, search_dir, specific_cluster = False, desired_cluster = None):
    
    ix_geo = edges_gdf.columns.get_loc("geometry")+1
    ix_name = edges_gdf.columns.get_loc("name")+1
    ix_u, ix_v = edges_gdf.columns.get_loc("u")+1, edges_gdf.columns.get_loc("v")+1
    
    u, v = edges_gdf.loc[ix_line]['u'], edges_gdf.loc[ix_line]['v']
    line = edges_gdf.loc[ix_line].geometry
    name = edges_gdf.loc[ix_line]['name']
    line_coords = list(line.coords)
    
    if search_dir == 'v': 
        coming_from = v
        other_node = u
        possible_matches = edges_gdf[(edges_gdf.u == v) | (edges_gdf.v == v)].copy()
    else: 
        line_coords.reverse()
        coming_from = u
        other_node = v
        possible_matches = edges_gdf[(edges_gdf.u == u) | (edges_gdf.v == u)].copy()
     
    possible_matches.drop(ix_line, axis = 0, inplace = True)
    nodes_traversed = []
    lines_traversed = []
    clusters_traversed = []
    last_line = ix_line

    found = False
    distance_start = 0.0

    if specific_cluster:
        cluster_geometry = clusters_gdf.loc[desired_cluster].geometry
        distance_start = cluster_geometry.distance(nodes_gdf.loc[coming_from].geometry)
    
    while not found:
        if len(possible_matches) == 0: 
            return(None, None, None, None, None, None)
        if specific_cluster:
            if cluster_geometry.distance(nodes_gdf.loc[coming_from].geometry) > distance_start:
                return(None, None, None, None, None, None)

        possible_matches.drop(last_line, axis = 0, errors = "ignore", inplace = True)
        if len(possible_matches) > 0:
            possible_matches['angle'] = 0.0
            for connector in possible_matches.itertuples():
                angle = angle_line_geometries(edges_gdf.loc[last_line].geometry, connector[ix_geo], deflection = True, degree = True)
                possible_matches.at[connector.Index, 'angle'] = angle
            
            possible_matches.sort_values(by = 'angle', ascending = True, inplace = True)   
            
        if len(possible_matches) == 0: 
            return(None, None, None, None, None, None)    
        for connector in possible_matches.itertuples():
            if not is_continuation(last_line, connector.Index, edges_gdf):
                possible_matches.drop(connector.Index, axis = 0, inplace = True)
                continue
            
            else:
                uCP, vCP = connector[ix_u], connector[ix_v]
                
                if uCP == coming_from:
                    cluster = nodes_gdf.loc[vCP].cluster
                    coming_from = vCP
                    distance_to = nodes_gdf.loc[vCP].geometry.distance(nodes_gdf.loc[other_node].geometry)
                    distance_from = nodes_gdf.loc[uCP].geometry.distance(nodes_gdf.loc[other_node].geometry)
                    if (vCP in nodes_traversed) | (distance_to < distance_from):
                        possible_matches = possible_matches[0:0]
                        break
                else: 
                    cluster = nodes_gdf.loc[uCP].cluster
                    coming_from = uCP
                    distance_to = nodes_gdf.loc[uCP].geometry.distance(nodes_gdf.loc[other_node].geometry)
                    distance_from = nodes_gdf.loc[vCP].geometry.distance(nodes_gdf.loc[other_node].geometry)
                    if (uCP in nodes_traversed) | (distance_to < distance_from):
                        possible_matches = possible_matches[0:0]
                        break
                                    
                if (cluster is None) | ((specific_cluster) & (cluster != desired_cluster)):
                    lines_traversed.append(connector.Index)
                    last_line = connector.Index

                    if vCP == coming_from:
                        possible_matches = edges_gdf[(edges_gdf.u == vCP) | (edges_gdf.v == vCP) ].copy()
                        nodes_traversed.append(uCP) 
                        line_coords = line_coords + list(connector[ix_geo].coords)
                    else:
                        possible_matches = edges_gdf[(edges_gdf.u == uCP) | (edges_gdf.v == uCP)].copy()
                        nodes_traversed.append(vCP)
                        tmp = list(connector[ix_geo].coords)
                        tmp.reverse()
                        line_coords = line_coords + tmp
                    if (specific_cluster) & (cluster is not None): 
                        clusters_traversed.append(cluster)
                    break
                
                elif (cluster is not None) | ((specific_cluster) & (cluster == desired_cluster)):
                    found = True
                    lines_traversed.append(connector.Index)
                    
                    if vCP == coming_from:
                        nodes_traversed.append(uCP)
                        last_node = vCP
                        line_coords = line_coords + list(connector[ix_geo].coords)
                    else: 
                        nodes_traversed.append(vCP)
                        last_node = uCP
                        tmp = list(connector[ix_geo].coords)
                        tmp.reverse()
                        line_coords = line_coords + tmp
                    break    
    merged_line = LineString([coor for coor in line_coords])  
    if ((len(clusters_traversed) == 0) & (specific_cluster)):
        for n in nodes_traversed:
            if nodes_gdf.loc[n].cluster is not None:
                clusters_traversed.append(nodes_gdf.loc[n].cluster)
            
    return(cluster, merged_line, lines_traversed, nodes_traversed, last_node, clusters_traversed)

def center_line_cluster(line_geometries, nodes_gdf, clusters_gdf, cluster_from, cluster_to, one_cluster = False):
    """
    Given two LineStrings, it derives the corresponding center line
    
    Parameters
    ----------
    line_geometries: list of LineString 
        the list of two LineString line
    nodes_gdf: LineString
        the second line
    clusters_gdf
    
    cluster_from, 
    
    cluster_to
    
    
    Returns:
    ----------
    center_line: LineString
        the resulting center line
    """
    
    line_geometry_A = line_geometries[0]
    line_geometry_B = line_geometries[1]
    if line_geometry_A.centroid.distance(line_geometry_B.centroid)> 100: 
        return None
    if one_cluster: 
        coord_from = (nodes_gdf.loc[cluster_from]['x'], nodes_gdf.loc[cluster_from]['y'])
    else: 
        coord_from = (clusters_gdf.loc[cluster_from]['x'], clusters_gdf.loc[cluster_from]['y'])
    
    coord_to =  (clusters_gdf.loc[cluster_to]['x'], clusters_gdf.loc[cluster_to]['y'])
    cl_coords = center_line_coords(line_geometry_A, line_geometry_B)
        
    cl_coords[0] = coord_from
    cl_coords[-1] = coord_to
    center_line = LineString([coor for coor in cl_coords])           
        
    return center_line        
                                                                                                                                                                                                                
def dissolve_dual_lines(ix_lines, line_geometries, nodes_gdf, edges_gdf, clusters_gdf, cluster, goal, first_node, last_node, 
                        nodes_traversed, direction, one_cluster = False, clusters_traversed = []):
    
    ix_lineA = ix_lines[0]
    ix_lineB = ix_lines[1]
    line_geometry_A = line_geometries[0]
    line_geometry_B = line_geometries[1]

    interpolation = len(nodes_traversed) > 0

    if not one_cluster:
        if ((edges_gdf.loc[ix_lineA]['name'] is not None) & (edges_gdf.loc[ix_lineB]['name'] is not None) & 
                    (edges_gdf.loc[ix_lineA]['name'] != edges_gdf.loc[ix_lineB]['name'])): 
            return None
    if ((line_geometry_A.length > line_geometry_B.length*1.50) | (line_geometry_B.length > line_geometry_A.length*1.50)): 
        return None
    
    if not one_cluster:    
        if (Point(line_geometry_A.coords[0]).distance(Point(line_geometry_A.coords[0])) >
                    Point(line_geometry_A.coords[0]).distance(Point(line_geometry_B.coords[-1]))):
            dist_SS = Point(line_geometry_A.coords[0]).distance(Point(line_geometry_B.coords[-1]))
            dist_EE = Point(line_geometry_A.coords[-1]).distance(Point(line_geometry_B.coords[0]))
        else:
            dist_SS = Point(line_geometry_A.coords[0]).distance(Point(line_geometry_B.coords[0]))
            dist_EE = Point(line_geometry_A.coords[-1]).distance(Point(line_geometry_B.coords[-1]))
            
        if (dist_SS > dist_EE*1.50) | (dist_EE > dist_SS*1.50): 
            return None
    
    if one_cluster: 
        cl = center_line_cluster(line_geometries, nodes_gdf, clusters_gdf, first_node, goal, one_cluster)
    else: 
        cl = center_line_cluster(line_geometries, nodes_gdf, clusters_gdf, cluster, goal)
    
    if cl is None: 
        return None
    if (direction == 'u') & (not interpolation):
        line_coords = list(cl.coords)
        line_coords.reverse() 
        cl = LineString([coor for coor in line_coords])
    if interpolation:
        interpolate_on_center_line(ix_lineA, cl, nodes_gdf, edges_gdf, first_node, last_node, nodes_traversed, clusters_gdf, clusters_traversed)
        return 'processed'
    
    edges_gdf.at[ix_lineA, 'new_geo'] = True
    edges_gdf.at[ix_lineA, 'geometry'] = cl
    
    return 'processed'


def dissolve_multiple_dual_lines(ix_lines, line_geometries, nodes_gdf, edges_gdf, clusters_gdf, cluster, goal, first_node, last_node, 
                                nodes_traversed, direction, one_cluster = False, clusters_traversed = []):
      
    dict_lines = dict(zip(ix_lines, line_geometries))
    secondary_lines = []
    max_dist = 0
    
    interpolation = len(nodes_traversed) > 0
    
    for line in dict_lines.values():
        for other_line in dict_lines.values():
            if line == other_line: 
                continue
            if line.length > other_line.length * 1.50: 
                return None     
    
    if (len(dict_lines)%2 == 0):
        while len(dict_lines) > 2:
            distances = {}
            for key, line in dict_lines.items():
                cumulative_distance = 0.0
                for other_key, other_line in dict_lines.items():
                    if key == other_key: 
                        continue
                    mid_point = line.interpolate(0.5, normalized = True)
                    other_mid_point = other_line.interpolate(0.5, normalized = True)
                    distance = mid_point.distance(other_mid_point)
                    cumulative_distance += distance
                    
                mean_distance = cumulative_distance/len(dict_lines)
                distances[key] = mean_distance
            distances = {k: v for k, v in sorted(distances.items(), key=lambda item: item[1])}
            to_remove = list(distances.keys())[-2:]
            for key in to_remove: 
                del dict_lines[key]
            
        line_geometries = list(dict_lines.values())
        
        if one_cluster: 
            cl = center_line_cluster(line_geometries, nodes_gdf, clusters_gdf, first_node, goal, one_cluster = True)
        else: 
            cl = center_line_cluster(line_geometries, nodes_gdf, clusters_gdf, cluster, goal)
        
    elif len(dict_lines)%2 != 0:   
        
        while len(dict_lines) > 3:
            distances = {}
            for key, line in dict_lines.items():
                cumulative_distance = 0.0
                for other_key, other_line in dict_lines.items():
                    if key == other_key: 
                        continue
                    mid_point = line.interpolate(0.5, normalized = True)
                    other_mid_point = other_line.interpolate(0.5, normalized = True)
                    distance = mid_point.distance(other_mid_point)
                    cumulative_distance += distance
                    
                mean_distance = cumulative_distance/len(dict_lines)
                distances[key] = mean_distance
            distances = {k: v for k, v in sorted(distances.items(), key=lambda item: item[1])}
            to_remove = list(distances.keys())[-2:]
            for key in to_remove: 
                del dict_lines[key]

        for key, line in dict_lines.items():
            for other_key, other_line in dict_lines.items():
                if key == other_key: 
                    continue
                mid_point = line.interpolate(0.5, normalized = True)
                other_mid_point = other_line.interpolate(0.5, normalized = True)
                distance = mid_point.distance(other_mid_point)
                if distance > max_dist: 
                    max_dist = distance
                    secondary_lines = [key, other_key]

        ix_central = [x for x in list(dict_lines.keys()) if x not in secondary_lines][0]
        cl = dict_lines[ix_central]
    
    if (direction == 'u') & (not interpolation):
        line_coords = list(cl.coords)
        line_coords.reverse() 
        cl = LineString([coor for coor in line_coords])

    if interpolation:
        interpolate_on_center_line(ix_lines[0], cl, nodes_gdf, edges_gdf, first_node, last_node, nodes_traversed, clusters_gdf, clusters_traversed) 
    else: 
        edges_gdf.at[ix_lines[0], 'geometry'] = cl
        edges_gdf.at[ix_lines[0], 'new_geo'] = True
        
    return 'processed'    
  
def is_possible_dual(ix_lineA, ix_lineB, edges_gdf, processed, one_cluster = False):
    
    line_geometry_A = edges_gdf.loc[ix_lineA].geometry
    line_geometry_B = edges_gdf.loc[ix_lineB].geometry
    
    if ix_lineB in processed: 
        return False
    if not one_cluster:
        if ((edges_gdf.loc[ix_lineA].u == edges_gdf.loc[ix_lineB].u) | (edges_gdf.loc[ix_lineA].u == edges_gdf.loc[ix_lineB].v)
            | (edges_gdf.loc[ix_lineA].v == edges_gdf.loc[ix_lineB].u) | (edges_gdf.loc[ix_lineA].v == edges_gdf.loc[ix_lineB].v)): 
            return False
        if not is_parallel(line_geometry_A, line_geometry_B, hard = True): 
            return False
    else: 
        if is_continuation(ix_lineA, ix_lineB, edges_gdf): 
            return False

    return True
 

def simplify_dual_lines(nodes_gdf, edges_gdf, clusters_gdf):
    
    nodes_gdf, edges_gdf, clusters_gdf = nodes_gdf.copy(), edges_gdf.copy(), clusters_gdf.copy()
    nodes_gdf, edges_gdf, clusters_gdf = _check_indexes(nodes_gdf, edges_gdf, clusters_gdf)
    
    
    ix_geo = edges_gdf.columns.get_loc("geometry")+1
    ix_u, ix_v  = edges_gdf.columns.get_loc("u")+1, edges_gdf.columns.get_loc("v")+1
    ix_name = edges_gdf.columns.get_loc("name")+1
    ix_cluster = nodes_gdf.columns.get_loc("cluster")+1
    ix_clus_u, ix_clus_v  = edges_gdf.columns.get_loc("clus_u")+1, edges_gdf.columns.get_loc("clus_v")+1
    ix_clus_uR, ix_clus_vR = edges_gdf.columns.get_loc("clus_uR")+1, edges_gdf.columns.get_loc("clus_vR")+1
    
    ################################ FROM NODES TO CLUSTERED JUNCTIONS
    
    clusters_gdf['keep'] = False
    edges_gdf['new_geo'] = False
    edges_gdf['forced_cluster'] = False
    original_nodes_gdf, original_edges_gdf, original_clusters_gdf = nodes_gdf.copy(), edges_gdf.copy(), clusters_gdf.copy()
    processed = []
    to_drop = []
    
    
    print('Simplifying dual lines: First part - clusters')
    clusters_gdf.sort_values(by = 'degree', ascending = False, inplace = True)
    list_cluster = clusters_gdf.index.values.tolist() 
    
    for cluster in list_cluster:
        edges_tmp = original_edges_gdf[((original_edges_gdf.clus_u == cluster) | (original_edges_gdf.clus_v == cluster))].copy()
        edges_tmp = edges_tmp[edges_tmp.clus_u != edges_tmp.clus_v].copy()
        edges_tmp.sort_values(by = 'length', ascending = False, inplace = True)
        if len(edges_tmp) == 1: 
            continue

        for road in edges_tmp.itertuples():          
            if road.Index in processed: 
                continue
            possible_dual_lines = edges_tmp.copy() 
            edges_gdf['forced_cluster'] = False
            
            # disregard unparallel lines 
            possible_dual_lines['candidate'] = True
            possible_dual_lines['candidate'] = possible_dual_lines.apply(lambda r: is_possible_dual(road.Index, r['edgeID'], original_edges_gdf, 
                                                        processed), axis = 1)
            possible_dual_lines.at[road.Index, 'candidate' ] = True
            possible_dual_lines = possible_dual_lines[possible_dual_lines.candidate]
            if len(possible_dual_lines) < 2: 
                continue
            possible_dual_lines['dir'] = 'v'

            # orientate everything from "u" to "v" in relation to the cluster
            for candidate in possible_dual_lines.itertuples():
                if candidate[ix_clus_v] == cluster:
                    line_coords = list(candidate[ix_geo].coords)
                    line_coords.reverse() 
                    new_line_geometry = LineString([coor for coor in line_coords])
                    old_u = candidate[ix_u]
                    old_clus_u, old_clus_uR = candidate[ix_clus_u], candidate[ix_clus_uR]
                    
                    possible_dual_lines.at[candidate.Index,'geometry'] = new_line_geometry
                    possible_dual_lines.at[candidate.Index,'u']  = candidate[ix_v]
                    possible_dual_lines.at[candidate.Index,'v'] = old_u
                    possible_dual_lines.at[candidate.Index,'clus_u'] = candidate[ix_clus_v]
                    possible_dual_lines.at[candidate.Index,'clus_v'] = old_clus_u
                    possible_dual_lines.at[candidate.Index,'clus_uR'] = candidate[ix_clus_vR]
                    possible_dual_lines.at[candidate.Index,'clus_vR'] = old_clus_uR
                    possible_dual_lines.at[candidate.Index, 'dir'] = 'u' # indicates original dir
            
            # does the line considered in the loop reach a cluster? if not straight away, at some point?            
            if possible_dual_lines.loc[road.Index]['clus_v'] is not None: 
                goal = possible_dual_lines.loc[road.Index]['clus_v']
            else: goal = possible_dual_lines.loc[road.Index]['clus_vR']
            if (goal is None) | (goal == cluster): 
                continue
            
            for candidate in possible_dual_lines.itertuples():
                
                if candidate[ix_clus_v] is not None: 
                    secondary_goal = candidate[ix_clus_v]
                else: secondary_goal = candidate[ix_clus_vR]
                if secondary_goal != goal: 
                    direction = possible_dual_lines.at[candidate.Index, 'dir']
                    forced_cluster = indirect_cluster(original_nodes_gdf, original_edges_gdf, original_clusters_gdf, candidate.Index, direction, 
                                specific_cluster = True, desired_cluster = goal)[0]     
                    if forced_cluster == goal:
                        possible_dual_lines.at[candidate.Index, 'forced_cluster'] = True
                        possible_dual_lines.at[candidate.Index, 'clus_vR'] = forced_cluster
                        possible_dual_lines.at[candidate.Index, 'clus_v'] = None
                    else: possible_dual_lines.drop(candidate.Index, axis = 0, inplace = True)
            
            done = False
            lines_traversed = []
            if len(possible_dual_lines) == 1: 
                continue # no parallel streets to row.Index 
            

            line_geometries = [possible_dual_lines.iloc[i]['geometry'] for i in range(0, len(possible_dual_lines))]       
            ix_lines = [possible_dual_lines.iloc[i].edgeID for i in range(0, len(possible_dual_lines))]  
            c_u = [possible_dual_lines.iloc[i]['clus_u'] for i in range(0, len(possible_dual_lines))]
            c_v = [possible_dual_lines.iloc[i]['clus_v'] for i in range(0, len(possible_dual_lines))]
            u =  [possible_dual_lines.iloc[i]['u'] for i in range(0, len(possible_dual_lines))] 
            v =  [possible_dual_lines.iloc[i]['v'] for i in range(0, len(possible_dual_lines))]
            forced_cluster =  [possible_dual_lines.iloc[i]['forced_cluster'] for i in range(0, len(possible_dual_lines))]
            drs = [possible_dual_lines.iloc[i]['dir'] for i in range(0, len(possible_dual_lines))] 
            list_nodes_traversed = [[] for i in range(0, len(possible_dual_lines))]
            list_lines_traversed = [[] for i in range(0, len(possible_dual_lines))]
            list_clusters_traversed = [[] for i in range(0, len(possible_dual_lines))] 
            last_node, nodes_traversed, lines_traversed, clusters_traversed = None, [], [], []
            
            ######################################################## 
            ## OPTION 1: they all reach another cluster:

            if all(x == c_v[0] for x in c_v) & (not None in c_v):
                if len(possible_dual_lines) == 2:
                    merged = dissolve_dual_lines(ix_lines, line_geometries, nodes_gdf, edges_gdf, clusters_gdf, cluster, goal, u[0], last_node,
                                                        nodes_traversed, drs[0])
                else:
                    merged = dissolve_multiple_dual_lines(ix_lines, line_geometries, nodes_gdf, edges_gdf, clusters_gdf, cluster, goal, u[0], 
                                    last_node, nodes_traversed, drs[0])
                if merged is None: 
                    continue 
                done = True
            
            ######################################################## 
            ## OPTION 2: at least one does not reach the cluster:    
            
            elif None in c_v:
                # pre-check 
                if len(possible_dual_lines) > 2:
                    all_checked = False
                    
                    while not all_checked:
                        all_checked = True
                        for n, line in enumerate(line_geometries):
                            for nn, other_line in enumerate(line_geometries):
                                if n >= nn : 
                                    continue
                                if ((line.coords[0] == other_line.coords[0]) | (line.coords[0] == other_line.coords[-1]) |
                                    (line.coords[-1] == other_line.coords[0]) | (line.coords[-1] == other_line.coords[-1])):
                                    if line.length > other_line.length: 
                                        to_remove = n
                                    elif line.length < other_line.length: 
                                        to_remove = nn
                                    else: continue
                                    for ll in [c_u, c_v, u, v, drs, line_geometries, ix_lines, list_nodes_traversed, list_lines_traversed, 
                                        list_clusters_traversed, forced_cluster]: 
                                        del ll[to_remove]
                                    all_checked = False
                                    break
                            if not all_checked: 
                                break
                            
                if len(ix_lines) < 2: 
                    continue
                    
                for n, c in enumerate(c_v):
                    specific_cluster, desired_cluster = False, None
                    if c is None:
                        if forced_cluster[n]:
                           specific_cluster = True
                           desired_cluster = goal
                           
                        _, line_geometries[n], list_lines_traversed[n], list_nodes_traversed[n], last_node, list_clusters_traversed[n] = indirect_cluster(
                                    original_nodes_gdf, original_edges_gdf, original_clusters_gdf, ix_lines[n], drs[n], specific_cluster = specific_cluster, 
                                    desired_cluster = desired_cluster)
                
                if len(possible_dual_lines) > 2:
                    all_checked = False
                    
                    while not all_checked:
                        all_checked = True
                        for n, i in enumerate(list_lines_traversed):
                            for nn, ii in enumerate(list_lines_traversed):
                                if n >= nn: 
                                    continue 
                                if len(list(set(i).intersection(ii))) > 0: 
                                    for ll in [c_u, c_v, u, v, drs, line_geometries, ix_lines, list_nodes_traversed, list_lines_traversed,
                                                list_clusters_traversed, forced_cluster]: 
                                        del ll[nn]
                                    all_checked = False
                                    break
                            if not all_checked: 
                                break

                if len(ix_lines) < 2: 
                    continue
                
                # last node does not matter, as it will be reassigned to the relative cluster
                nodes_traversed = [item for items in list_nodes_traversed for item in items if item is not None]
                lines_traversed = [item for items in list_lines_traversed for item in items if item is not None]
                clusters_traversed = [item for items in list_clusters_traversed for item in items if item is not None]
                
                if len(possible_dual_lines) == 2:
                    common = list(set(list_lines_traversed[0]).intersection(list_lines_traversed[1]))
                    if len(common) > 0:
                        continue
                    else: 
                        merged = dissolve_dual_lines(ix_lines, line_geometries, nodes_gdf, edges_gdf, clusters_gdf, cluster, goal, u[0], last_node, 
                                                    nodes_traversed, drs[0], clusters_traversed = clusters_traversed)
                else:
                    merged = dissolve_multiple_dual_lines(ix_lines, line_geometries, nodes_gdf, edges_gdf, clusters_gdf, cluster, goal, u[0], last_node,
                                                nodes_traversed, drs[0], clusters_traversed = clusters_traversed)
                if merged is None:
                    continue
                    
                done = True

            if not done: 
                pass 
            else:  
                clusters = [cluster, goal]
                between = (
                        list(original_edges_gdf.index[(original_edges_gdf.u.isin(nodes_traversed)) & (original_edges_gdf.v.isin(nodes_traversed))])+
                        list(original_edges_gdf.index[(original_edges_gdf.clus_u.isin(clusters)) & (original_edges_gdf.v.isin(nodes_traversed))])+
                        list(original_edges_gdf.index[(original_edges_gdf.clus_v.isin(clusters)) & (original_edges_gdf.u.isin(nodes_traversed))])+ 
                        list(original_edges_gdf.index[(original_edges_gdf.clus_uR.isin(clusters)) & (original_edges_gdf.v.isin(nodes_traversed))])+
                        list(original_edges_gdf.index[(original_edges_gdf.clus_vR.isin(clusters)) & (original_edges_gdf.u.isin(nodes_traversed))]))
                

                between = list(set(between + lines_traversed + ix_lines)) 
                to_drop = to_drop + between
                to_drop = list(filter(lambda a: a != ix_lines[0], to_drop)) 
                processed = processed + [ix_lines[0]] + to_drop
                clusters_gdf.at[clusters, 'keep'] =  True
                if len(original_edges_gdf.loc[processed][original_edges_gdf.pedestrian == 1]) > 0: 
                    edges_gdf.at[ix_lines[0], 'pedestrian'] = 1

    edges_gdf.drop(to_drop, axis = 0, inplace = True, errors = 'ignore')
    edges_gdf['edgeID'] = edges_gdf.index.values.astype(int)
    nodes_gdf['nodeID'] = nodes_gdf.index.values.astype(int)
    nodes_gdf, edges_gdf = reassign_edges(nodes_gdf, edges_gdf, clusters_gdf)   
    return(nodes_gdf, edges_gdf, clusters_gdf)    


def simplify_dual_lines_nodes_to_cluster(nodes_gdf, edges_gdf, clusters_gdf):
    
    nodes_gdf, edges_gdf, clusters_gdf = nodes_gdf.copy(), edges_gdf.copy(), clusters_gdf.copy()
    nodes_gdf, edges_gdf, clusters_gdf = _check_indexes(nodes_gdf, edges_gdf, clusters_gdf)
    
    processed = []
    print('Simplifying dual lines: Second part - nodes')
    edges_gdf = _assign_cluster_edges(nodes_gdf, edges_gdf, clusters_gdf)

    original_nodes_gdf, original_edges_gdf = nodes_gdf.copy(), edges_gdf.copy()
    ix_geo = edges_gdf.columns.get_loc("geometry")+1
    ix_u, ix_v  = edges_gdf.columns.get_loc("u")+1, edges_gdf.columns.get_loc("v")+1
    ix_name = edges_gdf.columns.get_loc("name")+1
    ix_cluster = nodes_gdf.columns.get_loc("cluster")+1
    ix_clus_u, ix_clus_v  = edges_gdf.columns.get_loc("clus_u")+1, edges_gdf.columns.get_loc("clus_v")+1
    ix_clus_uR, ix_clus_vR = edges_gdf.columns.get_loc("clus_uR")+1, edges_gdf.columns.get_loc("clus_vR")+1
    
    clusters_gdf['keep'] = False
    edges_gdf['new_geo'] = False
    to_drop = []
    
    for node in nodes_gdf.itertuples():
        tmp = original_edges_gdf[((original_edges_gdf.u == node[0]) | (original_edges_gdf.v == node[0]))].copy()
        
        for road in tmp.itertuples():
            if road.Index in processed: 
                continue 
            if road[ix_u] == node[0]:
                goal = road[ix_clus_v]
                if goal is None: 
                    goal = road[ix_clus_vR]
            elif road[ix_v] == node[0]:
                goal = road[ix_clus_u]
                if goal is None: 
                    goal = road[ix_clus_uR]
            if goal is None: 
                continue
                
            possible_dual_lines = tmp[(tmp.clus_u == goal) | (tmp.clus_uR == goal) | (tmp.clus_v == goal) | (tmp.clus_vR == goal)].copy()
            possible_dual_lines['dir'] = 'v'
            for candidate in possible_dual_lines.itertuples():
                if candidate[ix_v] == node[0]:
                    line_coords = list(candidate[ix_geo].coords)
                    line_coords.reverse() 
                    new_line_geometry = LineString([coor for coor in line_coords])
                    old_u, old_clus_u, old_clus_uR = candidate[ix_u], candidate[ix_clus_u], candidate[ix_clus_uR]
                    possible_dual_lines.at[candidate[0],'geometry'] = new_line_geometry
                    possible_dual_lines.at[candidate[0],'u'] = candidate[ix_v]
                    possible_dual_lines.at[candidate[0],'v'] = old_u
                    possible_dual_lines.at[candidate[0],'clus_u'] = candidate[ix_clus_v]
                    possible_dual_lines.at[candidate[0],'clus_v'] = old_clus_u
                    possible_dual_lines.at[candidate[0],'clus_uR'] = candidate[ix_clus_vR]
                    possible_dual_lines.at[candidate[0],'clus_vR'] = old_clus_uR
                    possible_dual_lines.at[candidate[0], 'dir'] = 'u' # indicates original dir
                
            possible_dual_lines = possible_dual_lines[(possible_dual_lines.clus_v == goal) | (possible_dual_lines.clus_vR == goal)].copy()

            done = False
            if len(possible_dual_lines) == 1: 
                continue # no parallel streets to road.Index          
            
            c_u = [possible_dual_lines.iloc[i]['clus_u'] for i in range(0, len(possible_dual_lines))]
            c_v = [possible_dual_lines.iloc[i]['clus_v'] for i in range(0, len(possible_dual_lines))]
            u =  [possible_dual_lines.iloc[i]['u'] for i in range(0, len(possible_dual_lines))] 
            v =  [possible_dual_lines.iloc[i]['v'] for i in range(0, len(possible_dual_lines))] 
            drs = [possible_dual_lines.iloc[i]['dir'] for i in range(0, len(possible_dual_lines))] 
            line_geometries = [possible_dual_lines.iloc[i]['geometry'] for i in range(0, len(possible_dual_lines))]       
            ix_lines = [possible_dual_lines.iloc[i].edgeID for i in range(0, len(possible_dual_lines))]      
            list_nodes_traversed = [[] for i in range(0, len(possible_dual_lines))]
            list_lines_traversed = [[] for i in range(0, len(possible_dual_lines))]    
            last_node, nodes_traversed, lines_traversed = None, [], []          
            
            ######################################################## OPTION 1
            if all(x == c_v[0] for x in c_v) & (not None in c_v):
                
                if len(possible_dual_lines) == 2:
                    merged = dissolve_dual_lines(ix_lines, line_geometries, nodes_gdf, edges_gdf, clusters_gdf, None, goal, u[0], last_node,
                                            nodes_traversed, drs[0], one_cluster = True)
                else:
                    merged = dissolve_multiple_dual_lines(ix_lines, line_geometries, nodes_gdf, edges_gdf, clusters_gdf, None, goal, u[0], 
                                    last_node, nodes_traversed, drs[0], one_cluster = True)
                if merged is None: 
                    # print('OPTION 1 -- NOT COMPLETED after having attempted to dissolve')
                    continue 

                done = True
                between = (list(original_edges_gdf.index[(original_edges_gdf.u.isin(nodes_traversed)) & 
                          (original_edges_gdf.v.isin(nodes_traversed))]))          
            
            ######################################################## OPTION 2
            elif None in c_v:

                for n, c in enumerate(c_v):
                    if c is None:
                        _, line_geometries[n], list_lines_traversed[n], list_nodes_traversed[n], last_node,_ = indirect_cluster(
                            original_nodes_gdf, original_edges_gdf, clusters_gdf, ix_lines[n], drs[n])
        
                nodes_traversed = [item for items in list_nodes_traversed for item in items if item is not None]
                lines_traversed = [item for items in list_lines_traversed for item in items if item is not None]
                if len(possible_dual_lines) == 2:
                    common = list(set(list_lines_traversed[0]).intersection(list_lines_traversed[1]))
                    if len(common) > 0:
                        continue
                    else: 
                        merged = dissolve_dual_lines(ix_lines, line_geometries, nodes_gdf, edges_gdf, clusters_gdf, None, goal, u[0], last_node, 
                                                    nodes_traversed, drs[0], one_cluster = True)
                else:
                    merged = dissolve_multiple_dual_lines(ix_lines, line_geometries, nodes_gdf, edges_gdf, clusters_gdf, None, goal, u[0], last_node,
                                                nodes_traversed, drs[0], one_cluster = True)
                if merged is None:
                    continue                  
                    
                done = True
                between = (list(original_edges_gdf.index[(original_edges_gdf.u.isin(nodes_traversed)) & 
                          (original_edges_gdf.v.isin(nodes_traversed))]))
                
            if not done: 
                continue
            to_drop = to_drop + lines_traversed + ix_lines + between
            to_drop = list(filter(lambda a: a != ix_lines[0], to_drop))
            processed = processed + [ix_lines[0]] + to_drop + lines_traversed + between    
            clusters_gdf.at[goal, 'keep'] = True
            if len(original_edges_gdf.loc[processed][original_edges_gdf.pedestrian == 1]) > 0:
                edges_gdf.at[ix_lines[0], 'pedestrian'] = 1

    edges_gdf.drop(to_drop, axis = 0, inplace = True, errors = 'ignore')
    nodes_gdf, edges_gdf = reassign_edges(nodes_gdf, edges_gdf, clusters_gdf)            
    edges_gdf['edgeID'] = edges_gdf.index.values.astype(int)
    nodes_gdf['nodeID'] = nodes_gdf.index.values.astype(int)
    nodes_gdf.drop(['cluster'], axis = 1, inplace = True)
    return(nodes_gdf, edges_gdf)

def reassign_edges(nodes_gdf, edges_gdf, clusters_gdf):
    
    print("Assigning centroids coordinates")
    nodes_gdf, edges_gdf = nodes_gdf.copy(), edges_gdf.copy()
    edges_gdf = edges_gdf.rename(columns = {'u':'old_u', 'v':'old_v'})
    
    edges_gdf['u'], edges_gdf['v'] = 0, 0
    ix_u, ix_v = edges_gdf.columns.get_loc("u")+1, edges_gdf.columns.get_loc("v")+1 
    ix_old_u, ix_old_v = edges_gdf.columns.get_loc("old_u")+1, edges_gdf.columns.get_loc("old_v")+1 
    ix_geo = edges_gdf.columns.get_loc("geometry")+1 
    ix_changed = edges_gdf.columns.get_loc("new_geo")+1 
    ix_cluster = nodes_gdf.columns.get_loc("cluster")+1 
    ix_x, ix_y = clusters_gdf.columns.get_loc("x")+1, clusters_gdf.columns.get_loc("y")+1
    ix_centroid = clusters_gdf.columns.get_loc("geometry")+1
    ix_check = clusters_gdf.columns.get_loc("keep")+1
    
    for row in edges_gdf.itertuples():
        
        line_coords = list(row[ix_geo].coords)
        u = nodes_gdf.loc[row[ix_old_u]]["cluster"]
        v = nodes_gdf.loc[row[ix_old_v]]["cluster"]
        old_u = row[ix_old_u]
        old_v = row[ix_old_v]
        new_geo = row[ix_changed]
        
        if (u is not None) & (v is not None):  # change starting and ending node in the list of coordinates for the line
            if (not clusters_gdf.loc[u].keep) & (not clusters_gdf.loc[v].keep): 
                u = old_u
                v = old_v
            elif not clusters_gdf.loc[v].keep:
                v = old_v
                line_coords[0] = (clusters_gdf.loc[u]['x'], clusters_gdf.loc[u]['y'])
                # if not new_geo: line_coords.insert(1,nodes_gdf.loc[row[ix_old_u]]['geometry'].coords[0]) 
            elif not clusters_gdf.loc[u].keep:
                u = old_u    
                line_coords[-1] = (clusters_gdf.loc[v]['x'], clusters_gdf.loc[v]['y'])
                # if not new_geo: line_coords.insert(-1,nodes_gdf.loc[row[ix_old_v]]['geometry'].coords[0]) 
            else:
                line_coords[0] = (clusters_gdf.loc[u]['x'], clusters_gdf.loc[u]['y'])
                line_coords[-1] = (clusters_gdf.loc[v]['x'], clusters_gdf.loc[v]['y'])
                # if not new_geo:
                    # line_coords.insert(1,nodes_gdf.loc[row[ix_old_u]]['geometry'].coords[0]) 
                    # line_coords.insert(-1,nodes_gdf.loc[row[ix_old_v]]['geometry'].coords[0]) 

        elif (u is None) & (v is None):  # maintain old_u and old_v
            u = old_u
            v = old_v
        elif (u is None) & (v is not None): # maintain old_u
            u = old_u
            if not clusters_gdf.loc[v].keep: 
                v = old_v
            else: 
                line_coords[-1] = (clusters_gdf.loc[v]['x'], clusters_gdf.loc[v]['y'])
                # if not new_geo: line_coords.insert(-1,nodes_gdf.loc[row[ix_old_v]]['geometry'].coords[0]) 
        elif (u is not None) & (v is None): # maintain old_v
            v = old_v
            if not clusters_gdf.loc[u].keep: 
                u = old_u
            else: 
                line_coords[0] = (clusters_gdf.loc[u]['x'], clusters_gdf.loc[u]['y'])
                # if not new_geo: line_coords.insert(1,nodes_gdf.loc[row[ix_old_u]]['geometry'].coords[0]) 
        
        line_geometry = (LineString([coor for coor in line_coords]))
        if u == v: 
            edges_gdf.drop(row.Index, axis = 0, inplace = True)
            continue
        
        edges_gdf.at[row.Index,"u"] = u
        edges_gdf.at[row.Index,"v"] = v
        edges_gdf.at[row.Index,"geometry"] = line_geometry

    edges_gdf.drop(['old_u', 'old_v'], axis = 1, inplace=True)
    edges_gdf['u'] = edges_gdf['u'].astype(int)
    edges_gdf['v'] = edges_gdf['v'].astype(int)
    nodes_gdf['x'] = nodes_gdf['x'].astype(float)
    nodes_gdf['y'] = nodes_gdf['y'].astype(float)
       
    for cluster in clusters_gdf.itertuples():
        if not cluster[ix_check]:
            continue
               
        nodes_gdf.at[cluster.Index, 'x'] = cluster[ix_x]
        nodes_gdf.at[cluster.Index, 'y'] = cluster[ix_y]
        nodes_gdf.at[cluster.Index, 'geometry'] = cluster[ix_centroid]
        nodes_gdf.at[cluster.Index, 'nodeID'] = cluster.Index
        nodes_gdf.at[cluster.Index, 'cluster'] = None
    
    clusters_gdf.index = clusters_gdf.clusterID.astype(int)
    nodes_gdf['nodeID'] = nodes_gdf.nodeID.astype(int)
    nodes_gdf.index = nodes_gdf.nodeID
    nodes_gdf.index.name = None
    edges_gdf.drop(['clus_u','clus_v', 'clus_uR', 'clus_vR', 'new_geo', 'forced_cluster'], axis = 1, errors = 'ignore', inplace = True)
    edges_gdf = correct_edges(nodes_gdf, edges_gdf)
    nodes_gdf, edges_gdf = clean_network(nodes_gdf, edges_gdf, dead_ends = True, remove_disconnected_islands = False, same_uv_edges = True, self_loops = True)
    return(nodes_gdf, edges_gdf)


def _check_indexes(nodes_gdf, edges_gdf, clusters_gdf):    
     
    nodes_gdf.index, edges_gdf.index, clusters_gdf.index = nodes_gdf.nodeID, edges_gdf.edgeID, clusters_gdf.clusterID
    nodes_gdf.index.name, edges_gdf.index.name, clusters_gdf.index.name = None, None, None
    
    return nodes_gdf, edges_gdf, clusters_gdf
          
def simplify_pipeline(nodes_gdf, edges_gdf, radius = 12):
    
    nodes_gdf, edges_gdf = nodes_gdf.copy(), edges_gdf.copy()
    nodes_gdf, edges_gdf = clean_network(nodes_gdf, edges_gdf, remove_disconnected_islands = True, same_uv_edges = True, dead_ends = True,
        self_loops = True)
    nodes_gdf, edges_gdf = simplify_dual_lines_junctions(nodes_gdf, edges_gdf)
    nodes_gdf, edges_gdf = simplify_complex_junctions(nodes_gdf, edges_gdf)
    nodes_gdf, edges_gdf = dissolve_roundabouts(nodes_gdf, edges_gdf)
    nodes_gdf, edges_gdf, clusters_gdf = identify_clusters(nodes_gdf, edges_gdf, radius = radius)
    nodes_gdf, edges_gdf, clusters_gdf = simplify_dual_lines(nodes_gdf, edges_gdf, clusters_gdf)
    nodes_gdf, edges_gdf = simplify_dual_lines_nodes_to_cluster(nodes_gdf, edges_gdf, clusters_gdf)
    nodes_gdf, edges_gdf = simplify_dual_lines_junctions(nodes_gdf, edges_gdf)
    nodes_gdf, edges_gdf = simplify_complex_junctions(nodes_gdf, edges_gdf)
    
    return nodes_gdf, edges_gdf
    
          
         
    
    
    
    
    
    
