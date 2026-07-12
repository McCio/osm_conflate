#!/usr/bin/env python3
import argparse
import contextlib
import csv
import json
import logging
import os
import shutil
import sys
from .geocoder import Geocoder
from .profile import Profile
from .conflator import OsmConflator, TITLE
from .dataset import (
    read_dataset,
    add_categories_to_dataset,
    transform_dataset,
    check_dataset_for_duplicates,
    add_regions,
)


def write_for_filter(profile, dataset, f):
    def query_to_tag_strings(query):
        if isinstance(query, str):
            raise ValueError('Query string for filter should not be a string')
        result = []
        if not isinstance(query[0], str) and isinstance(query[0][0], str):
            query = [query]
        for q in query:
            if isinstance(q, str):
                raise ValueError('Query string for filter should not be a string')
            parts = []
            for part in q:
                if len(part) == 1:
                    parts.append(part[0])
                elif part[1] is None or len(part[1]) == 0:
                    parts.append('{}='.format(part[0]))
                elif part[1][0] == '~':
                    raise ValueError('Cannot use regular expressions in filter')
                elif '|' in part[1] or ';' in part[1]:
                    raise ValueError('"|" and ";" symbols is not allowed in query values')
                else:
                    parts.append('='.join(part))
            result.append('|'.join(parts))
        return result

    def tags_to_query(tags):
        return [(k, v) for k, v in tags.items()]

    categories = profile.get('categories', {})
    p_query = profile.get('query', None)
    if p_query is not None:
        categories[None] = {'query': p_query}
    cat_map = {}
    i = 0
    try:
        for name, query in categories.items():
            for tags in query_to_tag_strings(query.get('query', tags_to_query(query.get('tags')))):
                f.write('{},{},{}\n'.format(i, name or '', tags))
            cat_map[name] = i
            i += 1
    except ValueError as e:
        logging.error(e)
        return False
    f.write('\n')
    for d in dataset:
        if d.category in cat_map:
            f.write('{},{},{}\n'.format(d.lon, d.lat, cat_map[d.category]))
    return True


@contextlib.contextmanager
def _as_file(arg, mode):
    """Yield arg as a file object; open it if it's a path string."""
    if arg is None:
        yield None
    elif isinstance(arg, (str, os.PathLike)):
        with open(arg, mode) as f:
            yield f
    else:
        yield arg


def run(
    profile=None,
    source=None,
    output=None,
    changes=None,
    osm=None,
    regions=None,
    overpass_url=None,
    alt_overpass=False,
    contact=None,
    osc=False,
    audit=None,
    param=None,
    check_move=False,
    for_filter=None,
    list_file=None,
    list_duplicates=False,
    verbose=False,
    quiet=False,
):
    """Run the conflation pipeline programmatically.

    File arguments (source, output, changes, audit, for_filter, list_file)
    accept either a path string/PathLike or an already-open file object.
    profile accepts a Profile object, a file object, or a path string.
    """
    if not output and not changes and not for_filter and not list_file:
        logging.error('No output specified (output, changes, for_filter, or list_file required)')
        return

    if verbose:
        log_level = logging.DEBUG
    elif quiet:
        log_level = logging.WARNING
    else:
        log_level = logging.INFO
    logging.basicConfig(level=log_level, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    with contextlib.ExitStack() as stack:
        src = stack.enter_context(_as_file(source, 'rb'))
        out = stack.enter_context(_as_file(output, 'w'))
        chg = stack.enter_context(_as_file(changes, 'w'))
        aud_f = stack.enter_context(_as_file(audit, 'r'))
        ff = stack.enter_context(_as_file(for_filter, 'w'))
        lst = stack.enter_context(_as_file(list_file, 'w'))

        if not isinstance(profile, Profile):
            logging.debug('Loading profile %s', profile)
            prof_f = stack.enter_context(_as_file(profile, 'r'))
            profile = Profile(prof_f, param)
        else:
            profile = Profile(profile, param) if param else profile

        aud = json.load(aud_f) if aud_f else None

        geocoder = Geocoder(profile.get_raw('regions'))
        if regions:
            geocoder.set_filter(regions)
        elif aud and aud.get('regions'):
            geocoder.set_filter(aud.get('regions'))

        dataset = read_dataset(profile, src)
        if not dataset:
            logging.error('Empty source dataset')
            sys.exit(2)
        transform_dataset(profile, dataset)
        add_categories_to_dataset(profile, dataset)
        check_dataset_for_duplicates(profile, dataset, list_duplicates)
        add_regions(dataset, geocoder)
        logging.info('Read %s items from the dataset', len(dataset))

        if ff:
            if write_for_filter(profile, dataset, ff):
                logging.info('Prepared data for filtering, exitting')
            return

        conflator = OsmConflator(profile, dataset, aud, contact=contact)
        conflator.geocoder = geocoder
        if overpass_url:
            conflator.set_overpass(overpass_url)
        elif alt_overpass:
            conflator.set_overpass('alt')

        osm_path = str(osm) if isinstance(osm, os.PathLike) else osm
        if osm_path and os.path.exists(osm_path):
            with open(osm_path, 'r') as f:
                conflator.parse_osm(f)
        else:
            bbox_cache_dir = osm_path + '.d' if osm_path else None
            conflator.download_osm(bbox_cache_dir=bbox_cache_dir)
            if len(conflator.osmdata) > 0 and osm_path:
                with open(osm_path, 'w') as f:
                    f.write(conflator.backup_osm())
                if bbox_cache_dir and os.path.isdir(bbox_cache_dir):
                    shutil.rmtree(bbox_cache_dir)
        logging.info('Downloaded %s objects from OSM', len(conflator.osmdata))

        conflator.match()
        auxiliary_tags = profile.get('auxiliary_tags', set())
        matched_cleaned = []
        for point in conflator.matched:
            new_tags = {key: val for key, val in point.tags.items() if key not in auxiliary_tags}
            point.tags = new_tags
            matched_cleaned.append(point)
        conflator.matched = matched_cleaned

        if out:
            diff = conflator.to_osc(not osc)
            out.write(diff)

        if chg:
            if check_move:
                conflator.check_moveability()
            fc = {'type': 'FeatureCollection', 'features': conflator.changes}
            json.dump(fc, chg, ensure_ascii=False, sort_keys=True, indent=1)

        if lst:
            writer = csv.writer(lst)
            writer.writerow(['ref', 'osm_type', 'osm_id', 'lat', 'lon', 'action'])
            for row in conflator.matches:
                writer.writerow(row)

    logging.info('Done')


def main():
    parser = argparse.ArgumentParser(
        description='''{}.
        Reads a profile with source data and conflates it with OpenStreetMap data.
        Produces an JOSM XML file ready to be uploaded.'''.format(TITLE))
    parser.add_argument('profile', type=argparse.FileType('r'),
                        help='Name of a profile (python or json) to use')
    parser.add_argument('-i', '--source', type=argparse.FileType('rb'),
                        help='Source file to pass to the profile dataset() function')
    parser.add_argument('-a', '--audit', type=argparse.FileType('r'),
                        help='Conflation validation result as a JSON file')
    parser.add_argument('-o', '--output', type=argparse.FileType('w'),
                        help='Output OSM XML file name')
    parser.add_argument('-p', '--param',
                        help='Optional parameter for the profile')
    parser.add_argument('--osc', action='store_true',
                        help='Produce an osmChange file instead of JOSM XML')
    parser.add_argument('--osm',
                        help='Instead of querying Overpass API, use this unpacked osm file. ' +
                        'Create one from Overpass data if not found')
    parser.add_argument('-c', '--changes', type=argparse.FileType('w'),
                        help='Write changes as GeoJSON for visualization')
    parser.add_argument('-m', '--check-move', action='store_true',
                        help='Check for moveability of modified modes')
    parser.add_argument('-f', '--for-filter', type=argparse.FileType('w'),
                        help='Prepare a file for the filtering script')
    parser.add_argument('-l', '--list', type=argparse.FileType('w'),
                        help='Print a CSV list of matches')
    parser.add_argument('-d', '--list_duplicates', action='store_true',
                        help='List all duplicate points in the dataset')
    parser.add_argument('-r', '--regions',
                        help='Conflate only points with regions in this comma-separated list')
    parser.add_argument('--alt-overpass', action='store_true',
                        help='Use an alternate Overpass API server')
    parser.add_argument('--overpass-url',
                        help='Query an Overpass server on this URL')
    parser.add_argument('--contact',
                        help='Contact reference included in the User-Agent header (e.g. a URL or email)')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Display debug messages')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='Do not display informational messages')
    options = parser.parse_args()

    run(
        profile=options.profile,
        source=options.source,
        output=options.output,
        changes=options.changes,
        osm=options.osm,
        regions=options.regions,
        overpass_url=options.overpass_url,
        alt_overpass=options.alt_overpass,
        contact=options.contact,
        osc=options.osc,
        audit=options.audit,
        param=options.param,
        check_move=options.check_move,
        for_filter=options.for_filter,
        list_file=options.list,
        list_duplicates=options.list_duplicates,
        verbose=options.verbose,
        quiet=options.quiet,
    )
