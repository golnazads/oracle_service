from __future__ import division
from builtins import chr
from builtins import map
from builtins import zip
from builtins import range

import sys
import re

from fuzzywuzzy import fuzz
import lxml.html
import unidecode

from flask import current_app

from oraclesrv.utils import get_a_record, get_a_matched_record
from oraclesrv.keras_model import KerasModel
from oraclesrv.models import DocMatch

confidence_model = KerasModel()

re_match_collaboration = re.compile(r'([Cc]ollaboration[s\s]*)')
def count_matching_authors(ref_authors, ads_authors):
    """

    :param ref_authors:
    :param ads_authors:
    :return:
    """
    missing_in_ref, missing_in_ads, matching_authors, first_author_missing = 0, 0, 0, False

    try:
        ref_authors = ref_authors.split(';')
        ref_authors_lastname = [a.split(",")[0].strip() for a in ref_authors]
        ref_authors_first_initial = [a.split(",")[1].strip()[0] if len(a.split(',')) >= 2 else '' for a in ref_authors]
        ref_authors_norm = [(last+', '+ first).strip() for last,first in zip(ref_authors_lastname,ref_authors_first_initial)]

        for author in ads_authors:
            if author in ref_authors_norm:
                matching_authors += 1
            else:
                missing_in_ref += 1

        for author in ref_authors_norm:
            if author not in ads_authors:
                missing_in_ads += 1

        first_author_missing = fuzz.partial_ratio(ads_authors[0], ref_authors_norm[0]) < current_app.config['ORACLE_SERVICE_FIRST_AUTHOR_MATCH_THRESHOLD']

        # because of formatting issues ref_authors might be missing first initials
        # hence if nothing matches, see if last names match
        # but need to penalize that only last names were matched
        if matching_authors == 0:
            ads_authors_lastname = [a.split(",")[0].strip() for a in ads_authors]
            matching_authors = round(len(set(ads_authors_lastname) & set(ref_authors_lastname)) * current_app.config['ORACLE_SERVICE_LAST_NAME_ONLY'])
            if matching_authors > 0:
                missing_in_ref = len(ref_authors_lastname) - matching_authors
                missing_in_ads = len(ads_authors_lastname) - matching_authors
    except Exception as e:
        current_app.logger.error(f"Error in counting authors: {e}")

    return (missing_in_ref, missing_in_ads, matching_authors, first_author_missing)

def get_author_score(ref_authors, ads_authors):
    """

    :param ref_authors:
    :param ads_authors:
    :return:
    """
    # note that ref_authors is a string, and we need to have at least one name to match it to
    # ads_authors with is a list, that should contain at least one name
    if len(ref_authors) == 0 or len(ads_authors) == 0:
        return 0

    # if there is collabration, consider the that only and return score for the first author only
    if re_match_collaboration.findall(ref_authors) and re_match_collaboration.findall(';'.join(ads_authors)):
        return 0.3

    (missing_in_ref, missing_in_ads, matching_authors, first_author_missing
     ) = count_matching_authors(ref_authors, ads_authors)

    # if the first author is missing, apply the factor by which matching authors are discounted
    if first_author_missing:
        matching_authors *= 0.3

    score = (matching_authors - abs(missing_in_ref - missing_in_ads)) / float(len(ads_authors))

    return round(max(0, min(1, score)),2)

def get_year_score(diff):
    """

    :param diff:
    :return:
    """
    if diff <= 1:
        return 1
    if diff <= 2:
        return 0.75
    if diff <= 3:
        return 0.5
    if diff <= 4:
        return 0.25
    return 0

def get_refereed_score(is_refereed):
    """

    :param is_refereed:
    :return:
    """
    if is_refereed:
        return current_app.config['ORACLE_SERVICE_REFEREED_SCORE']
    return current_app.config['ORACLE_SERVICE_NOT_REFEREED_SCORE']

def get_matches(source_bibcode, doctype, abstract, title, author, year, doi, matched_docs):
    """

    :param source_bibcode:
    :param doctype:
    :param abstract:
    :param title:
    :param author:
    :param year:
    :param doi:
    :param matched_docs:
    :return:
    """
    confidence_threshold = current_app.config['ORACLE_SERVICE_CONFIDENCE_THRESHOLD']
    confidence_difference = current_app.config['ORACLE_SERVICE_CONFIDENCE_DIFFERENCE']
    results = []
    for doc in matched_docs:
        match_bibcode = doc.get('bibcode', '')
        match_abstract = clean_metadata(doc.get('abstract', ''))
        match_title = clean_metadata(' '.join(doc.get('title', [])))
        match_author = doc.get('author_norm', [])
        match_year = doc.get('year', None)
        match_doi = doc.get('doi', [])
        match_identifier = doc.get('identifier', [])

        # see if there is a doi from eprint, and add it to the doi list
        doi_pubnote = doc.get('doi_pubnote', None)
        if doi_pubnote and doi_pubnote not in match_doi:
            match_doi.append(doi_pubnote)

        # if by any chance the same record has been returned skip it
        if source_bibcode == match_bibcode:
            continue

        if len(abstract) > 0 and not abstract.lower().startswith('not available') and len(match_abstract) > 0:
            scores = [
                fuzz.token_set_ratio(abstract, match_abstract) / 100.0,
                fuzz.partial_ratio(title, match_title) / 100.0,
                get_author_score(author, match_author),
                get_year_score(abs(int(match_year) - int(year)))
            ]
        else:
            scores = [
                None,
                fuzz.partial_ratio(title, match_title) / 100.0,
                get_author_score(author, match_author),
                get_year_score(abs(int(match_year) - int(year)))
            ]
        # include doi if there is a match
        if match_doi and doi:
            dois_matches = any(x in doi for x in match_doi)
        else:
            dois_matches = False
        # add the score of doi in, if matched
        if dois_matches:
            scores = scores + [1]
        # if no doi, no abstract, and authors do not match, ignore this match
        elif scores[0] == None and scores[2] == 0:
                continue

        confidence_format = '%.{}f'.format(current_app.config['ORACLE_SERVICE_CONFIDENCE_SIGNIFICANT_DIGITS'])
        # if we are matching with eprints, consider eprint a refereed manuscript
        # else check the flag for refereed in the property field
        # if not refereed we want to penalize the confidence score
        match_refereed = True if current_app.config['ORACLE_DOCTYPE_EPRINT'] in doc.get('doctype') else (True if 'REFEREED' in doc.get('property', []) else False)
        confidence = float(confidence_format % (confidence_model.predict(scores) * get_refereed_score(match_refereed)))

        # see if either of these bibcodes have already been matched
        prev_match = get_a_record(source_bibcode, match_bibcode)

        # if confidence is low, doi does not matches, and there is no prev matches, skip it
        if confidence < confidence_threshold and not dois_matches and not prev_match:
            continue

        if prev_match:
            prev_bibcodes = [prev_match['eprint_bibcode'], prev_match['pub_bibcode']]
            # if prev record is the current match and the bibcode has changed in the meantime
            if prev_match['eprint_bibcode'] in match_identifier or prev_match['pub_bibcode'] in match_identifier:
                prev_bibcodes += match_identifier

            prev_confidence = prev_match['confidence']
            # is it the same record being matched again
            if source_bibcode in prev_bibcodes and match_bibcode in prev_bibcodes and prev_confidence >= confidence:
                # return the confidence that is recorded in db
                confidence = prev_confidence
                # set the scores to empty, since these are not the similarity scores for the match anymore
                scores = []
            # if this match is marked as wrong in the database, ignore it
            elif source_bibcode == prev_match['eprint_bibcode'] and match_bibcode == prev_match['pub_bibcode'] and prev_match['confidence'] < 0:
                continue
            # if there was a match, but different from the current match,
            # see if the confidence is higher then the current match
            # and if yes, ignore current match
            elif (source_bibcode in prev_bibcodes or match_bibcode in prev_bibcodes) and prev_confidence > confidence:
                scores = []
                # either or both have been matched, so use the previous match
                # find out which way we are matching to assing it correctly
                if doctype == current_app.config['ORACLE_DOCTYPE_EPRINT'] and source_bibcode == prev_match['eprint_bibcode']:
                    match_bibcode = prev_match['pub_bibcode']
                    confidence = prev_confidence
                elif doctype == current_app.config['ORACLE_DOCTYPE_PUB'] and source_bibcode == prev_match['pub_bibcode']:
                    match_bibcode = prev_match['eprint_bibcode']
                    confidence = prev_confidence
                else:
                    continue

        result = {'source_bibcode': source_bibcode, 'matched_bibcode': match_bibcode,
                  'confidence': confidence, 'matched': int(confidence > 0.5),
                  'scores': {'abstract':scores[0], 'title': scores[1], 'author': scores[2], 'year': scores[3]} if scores else {}}
        if len(scores) == 5:
            result['scores'].update({'doi': scores[4]})
        if result not in results:
            results.append(result)

    if len(results) == 0:
        return []

    if len(results) == 1 and results[0]['confidence'] >= confidence_threshold:
        return results

    # if multiple records are returned, make sure highest is at the top, then remove any records that
    # have confidence difference with the largest > 0.5 or their confidence is lower than the threshold
    results = sorted(results, key=lambda x: x['confidence'], reverse=True)
    results = [results[0]] + [result for result in results[1:] if result['confidence'] >= confidence_threshold and (results[0]['confidence'] - result['confidence']) < confidence_difference]

    if (len(results) > 0 and results[0]['confidence'] >= confidence_threshold):
        return results

    return []

def get_doi_match(source_bibcode, doctype, abstract, title, author, year, doi, matched_docs):
    """

    :param source_bibcode:
    :param doctype:
    :param abstract:
    :param title:
    :param author:
    :param year:
    :param doi:
    :param matched_docs:
    :return:
    """
    results = get_matches(source_bibcode, doctype, abstract, title, author, year, doi, matched_docs)
    if len(results) == 1:
        return results
    return []

def get_db_match(source_bibcode):
    """

    :param source_bibcode:
    :return:
    """
    matched_doc = get_a_matched_record(source_bibcode)
    if matched_doc:
        matched_bibcode = matched_doc['pub_bibcode'] if source_bibcode != matched_doc['pub_bibcode'] else matched_doc['eprint_bibcode']
        return [{'source_bibcode': source_bibcode, 'matched_bibcode': matched_bibcode,
                  'confidence': matched_doc['confidence'], 'matched': 1, 'scores': {}}]
    return []

def get_illegal_char_regex():
    """
    Returns an re object to find unicode characters illegal in XML

    :return:
    """
    illegal_unichrs = [ (0x00, 0x08), (0x0B, 0x1F), (0x7F, 0x84), (0x86, 0x9F),
        (0xD800, 0xDFFF), (0xFDD0, 0xFDDF), (0xFFFE, 0xFFFF),
        (0x1FFFE, 0x1FFFF), (0x2FFFE, 0x2FFFF), (0x3FFFE, 0x3FFFF),
        (0x4FFFE, 0x4FFFF), (0x5FFFE, 0x5FFFF), (0x6FFFE, 0x6FFFF),
        (0x7FFFE, 0x7FFFF), (0x8FFFE, 0x8FFFF), (0x9FFFE, 0x9FFFF),
        (0xAFFFE, 0xAFFFF), (0xBFFFE, 0xBFFFF), (0xCFFFE, 0xCFFFF),
        (0xDFFFE, 0xDFFFF), (0xEFFFE, 0xEFFFF), (0xFFFFE, 0xFFFFF),
        (0x10FFFE, 0x10FFFF) ]
    illegal_ranges = ["%s-%s" % (chr(low), chr(high))
        for (low, high) in illegal_unichrs
        if low < sys.maxunicode]
    return re.compile(u'[%s]' % u''.join(illegal_ranges))
ILLEGALCHARSREGEX = get_illegal_char_regex()

re_illegal_xml = re.compile(u'([\u0000-\u0008\u000b-\u000c\u000e-\u001f\ufffe-\uffff])|' + \
                            u'([%s-%s][^%s-%s])|([^%s-%s][%s-%s])|([%s-%s]$)|(^[%s-%s])' % \
                            (chr(0xd800), chr(0xdbff), chr(0xdc00), chr(0xdfff),
                             chr(0xd800), chr(0xdbff), chr(0xdc00), chr(0xdfff),
                             chr(0xd800), chr(0xdbff), chr(0xdc00), chr(0xdfff)))
re_white_space = re.compile(r"\s+")
re_control_char = re.compile(r"[\x01-\x08\x0B-\x1F\x7F]")


def remove_control_chars(input, strict=False):
    """

    :param input:
    :param strict:
    :return:
    """
    input = re_illegal_xml.sub("", input)
    if not strict:
        # map all whitespace to single blank
        input = re_white_space.sub(" ", input)
    # now remove control characters
    input = re_control_char.sub("", input)
    return input

re_latex_math = re.compile(r'(\$[^$]*\$)')
re_html_entity = re.compile(r'(</?SU[BP]>)', re.IGNORECASE)
re_escape = re.compile(r'(\\\s*\w+|\\\s*\W+)\b')
def strip_latex_html(input):
    """

    :param input:
    :return:
    """
    output = re_latex_math.sub('', input)
    # if the entire text was marked as math, remove the dollar signs and ignore that it was a latex math
    if not output:
        output = input.replace('$', '')
    output = re_html_entity.sub('', output)
    output = re_escape.sub('', output)
    return output

def clean_metadata(input):
    """

    :param input:
    :return:
    """
    # check if there are invalid unicode characters
    if ILLEGALCHARSREGEX.search(input):
        # strip illegal stuff but keep newlines
        current_app.logger.error('Illegal unicode character in found %s' %input)
        input = remove_control_chars(input).strip()

    output = input.replace(' \n', '').replace('\n', '').replace(' <P/>', '').rstrip('\\')
    output = output.strip().replace('"', '')

    # remove any latex or html tags
    output = strip_latex_html(output)

    return output

def sub_entity(match):
    """

    :param match:
    :return:
    """
    unicode_dict = current_app.config['ORACLE_SERVICE_UNICODE_CONVERSION']

    key = match.group(1)
    if unicode_dict.get(key, None) is not None:
        result = eval("u'\\u%04x'" % unicode_dict[key])
        return result
    return None


RE_ENTITY = re.compile(r'&([^#][^; ]+?);')
def to_unicode(input):
    """

    :param input:
    :return:
    """
    retstr = RE_ENTITY.sub(sub_entity, input)
    return retstr


CONTROL_CHAR_RE = re.compile(r'[%s]' % re.escape(''.join(map(chr, list(range(0,32)) + list(range(127,160))))))
def remove_control_chars_author(input):
    """

    :param input:
    :return:
    """
    return CONTROL_CHAR_RE.sub('', input)

def encode_author(author):
    """

    :param author:
    :return:
    """
    try:
        author = lxml.html.fromstring(author).text
        if isinstance(author, str):
            return unidecode.unidecode(remove_control_chars_author(to_unicode(author)))
    except:
        return author

RE_INITIAL = re.compile(r'\. *(?!,)')
def format_author(author):
    """

    :param author:
    :return:
    """
    author = RE_INITIAL.sub('. ', author)
    # Strip potentially disastrous semicolons.
    return author.strip().strip(';')