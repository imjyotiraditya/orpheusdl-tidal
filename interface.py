import base64
import json
import logging
import re
from getpass import getpass
from dataclasses import dataclass
from shutil import copyfileobj
from xml.etree import ElementTree

import ffmpeg
from tqdm import tqdm

from utils.models import *
from utils.utils import sanitise_name, silentremove, download_to_temp, create_temp_filename
from .tidal_api import TidalTvSession, TidalApi, SessionStorage, TidalMobileSession, SessionType

module_information = ModuleInformation(
    service_name='Tidal',
    module_supported_modes=ModuleModes.download | ModuleModes.credits | ModuleModes.covers | ModuleModes.lyrics,
    login_behaviour=ManualEnum.manual,
    global_settings={
        'tv_token': '7m7Ap0JC9j1cOM3n',
        'tv_secret': 'vRAdA108tlvkJpTsGZS8rGZ7xTlbJ0qaZ2K9saEzsgY=',
        'mobile_token': 'dN2N95wCyEBTllu4',
        'enable_mobile': True,
        'prefer_ac4': False
    },
    session_storage_variables=[SessionType.TV.name, SessionType.MOBILE.name],
    netlocation_constant='tidal',
    test_url='https://tidal.com/browse/track/92265335'
)


@dataclass
class AudioTrack:
    codec: CodecEnum
    sample_rate: int
    bitrate: int
    urls: list


class ModuleInterface:
    # noinspection PyTypeChecker
    def __init__(self, module_controller: ModuleController):
        self.cover_size = module_controller.orpheus_options.default_cover_options.resolution
        self.oprinter = module_controller.printer_controller
        self.print = module_controller.printer_controller.oprint
        self.disable_subscription_check = module_controller.orpheus_options.disable_subscription_check
        self.prefer_ac4 = module_controller.module_settings['prefer_ac4']

        settings = module_controller.module_settings

        # LOW = 96kbit/s AAC, HIGH = 320kbit/s AAC, LOSSLESS = 44.1/16 FLAC, HI_RES <= 48/24 FLAC with MQA
        self.quality_parse = {
            QualityEnum.LOW: 'LOW',
            QualityEnum.MEDIUM: 'HIGH',
            QualityEnum.HIGH: 'HIGH',
            QualityEnum.LOSSLESS: 'LOSSLESS',
            QualityEnum.HIFI: 'HI_RES'
        }

        sessions = {}
        self.available_sessions = [SessionType.TV.name, SessionType.MOBILE.name]

        if settings['enable_mobile']:
            storage: SessionStorage = module_controller.temporary_settings_controller.read(SessionType.MOBILE.name)
            if not storage:
                confirm = input(' "enable_mobile" is enabled but no MOBILE session was found. Do you want to create a '
                                'MOBILE session (used for AC-4/360RA) [Y/n]? ')
                if confirm.upper() == 'N':
                    self.available_sessions = [SessionType.TV.name]
        else:
            self.available_sessions = [SessionType.TV.name]

        for session_type in self.available_sessions:
            storage: SessionStorage = module_controller.temporary_settings_controller.read(session_type)

            if session_type == SessionType.TV.name:
                sessions[session_type] = TidalTvSession(settings['tv_token'], settings['tv_secret'])
            else:
                sessions[session_type] = TidalMobileSession(settings['mobile_token'])

            if storage:
                logging.debug(f'Tidal: {session_type} session found, loading')

                sessions[session_type].set_storage(storage)
            else:
                logging.debug(f'Tidal: No {session_type} session found, creating new one')
                if session_type == SessionType.TV.name:
                    sessions[session_type].auth()
                else:
                    self.print('Tidal: Enter your Tidal username and password:')
                    username = input(' Username: ')
                    password = getpass(' Password: ')
                    sessions[session_type].auth(username, password)
                    self.print('Successfully logged in!')

                module_controller.temporary_settings_controller.set(session_type, sessions[session_type].get_storage())

            # Always try to refresh session
            if not sessions[session_type].valid():
                sessions[session_type].refresh()
                # Save the refreshed session in the temporary settings
                module_controller.temporary_settings_controller.set(session_type, sessions[session_type].get_storage())

            while True:
                # check for a valid subscription
                subscription = self.check_subscription(sessions[session_type].get_subscription())
                if subscription:
                    break

                confirm = input(' Do you want to create a new session? [Y/n]: ')

                if confirm.upper() == 'N':
                    self.print('Exiting...')
                    exit()

                # create a new session finally
                if session_type == SessionType.TV.name:
                    sessions[session_type].auth()
                else:
                    self.print('Tidal: Enter your Tidal username and password:')
                    username = input('Username: ')
                    password = getpass('Password: ')
                    sessions[session_type].auth(username, password)

                module_controller.temporary_settings_controller.set(session_type,
                                                                    sessions[session_type].get_storage())

        self.session: TidalApi = TidalApi(sessions)

    def check_subscription(self, subscription: str) -> bool:
        # returns true if "disable_subscription_checks" is enabled or subscription is HIFI Plus
        if not self.disable_subscription_check and subscription not in {'HIFI', 'PREMIUM_PLUS'}:
            self.print(f'Tidal: Account is not a HiFi Plus account, detected subscription: {subscription}')
            return False
        return True

    @staticmethod
    def generate_artwork_url(cover_id: str, size: int, max_size: int = 1280):
        # not the best idea, but it rounds the self.cover_size to the nearest number in supported_sizes, 1281 is needed
        # for the "uncompressed" cover
        supported_sizes = [80, 160, 320, 480, 640, 1080, 1280, 1281]
        best_size = min(supported_sizes, key=lambda x: abs(x - size))
        # only supports 80x80, 160x160, 320x320, 480x480, 640x640, 1080x1080 and 1280x1280 only for non playlists
        # return "uncompressed" cover if self.cover_resolution > max_size
        image_name = '{0}x{0}.jpg'.format(best_size) if best_size <= max_size else 'origin.jpg'
        return f'https://resources.tidal.com/images/{cover_id.replace("-", "/")}/{image_name}'

    @staticmethod
    def generate_animated_artwork_url(cover_id: str, size=1280):
        return 'https://resources.tidal.com/videos/{0}/{1}x{1}.mp4'.format(cover_id.replace('-', '/'), size)

    def search(self, query_type: DownloadTypeEnum, query: str, track_info: TrackInfo = None, limit: int = 20):
        results = self.session.get_search_data(query, limit=limit)

        items = []
        for i in results[query_type.name + 's']['items']:
            if query_type is DownloadTypeEnum.artist:
                name = i['name']
                artists = None
                year = None
            elif query_type is DownloadTypeEnum.playlist:
                name = i['title']
                artists = [i['creator']['name']]
                year = ""
            elif query_type is DownloadTypeEnum.track:
                name = i['title']
                artists = [j['name'] for j in i['artists']]
                # Getting the year from the album?
                year = i['album']['releaseDate'][:4]
            elif query_type is DownloadTypeEnum.album:
                name = i['title']
                artists = [j['name'] for j in i['artists']]
                year = i['releaseDate'][:4]
            else:
                raise Exception('Query type is invalid')

            additional = None
            if query_type is not DownloadTypeEnum.artist:
                if i['audioModes'] == ['DOLBY_ATMOS']:
                    additional = "Dolby Atmos"
                elif i['audioModes'] == ['SONY_360RA']:
                    additional = "360 Reality Audio"
                elif i['audioQuality'] == 'HI_RES':
                    additional = "MQA"
                else:
                    additional = 'HiFi'

            item = SearchResult(
                name=name,
                artists=artists,
                year=year,
                result_id=str(i['id']),
                explicit=bool(i['explicit']) if 'explicit' in i else None,
                additional=[additional] if additional else None
            )

            items.append(item)

        return items

    def get_playlist_info(self, playlist_id: str) -> PlaylistInfo:
        playlist_data = self.session.get_playlist(playlist_id)
        playlist_tracks = self.session.get_playlist_items(playlist_id)

        tracks = [track['item']['id'] for track in playlist_tracks['items'] if track['type'] == 'track']

        if 'name' in playlist_data['creator']:
            creator_name = playlist_data['creator']['name']
        elif playlist_data['creator']['id'] == 0:
            creator_name = 'TIDAL'
        else:
            creator_name = 'Unknown'

        return PlaylistInfo(
            name=playlist_data['title'],
            creator=creator_name,
            tracks=tracks,
            # TODO: Use playlist creation date or lastUpdated?
            release_year=playlist_data['created'][:4],
            creator_id=playlist_data['creator']['id'],
            cover_url=self.generate_artwork_url(playlist_data['squareImage'], size=self.cover_size, max_size=1080),
            track_extra_kwargs={'data': {track['item']['id']: track['item'] for track in playlist_tracks['items']}}
        )

    def get_artist_info(self, artist_id: str, get_credited_albums: bool) -> ArtistInfo:
        artist_data = self.session.get_artist(artist_id)

        artist_albums = self.session.get_artist_albums(artist_id)['items']
        artist_singles = self.session.get_artist_albums_ep_singles(artist_id)['items']

        # Only works with a mobile session, annoying, never do this again
        credit_albums = []
        if get_credited_albums and SessionType.MOBILE.name in self.available_sessions:
            self.session.default = SessionType.MOBILE
            credited_albums_page = self.session.get_page('contributor', params={'artistId': artist_id})

            # This is so retarded
            page_list = credited_albums_page['rows'][-1]['modules'][0]['pagedList']
            total_items = page_list['totalNumberOfItems']
            more_items_link = page_list['dataApiPath'][6:]

            # Now fetch all the found total_items
            items = []
            for offset in range(0, total_items // 50 + 1):
                print(f'Fetching {offset * 50}/{total_items}', end='\r')
                items += self.session.get_page(more_items_link, params={'limit': 50, 'offset': offset * 50})['items']

            credit_albums = [item['item']['album'] for item in items]
            self.session.default = SessionType.TV

        albums = [str(album['id']) for album in artist_albums + artist_singles + credit_albums]

        return ArtistInfo(
            name=artist_data['name'],
            albums=albums,
            album_extra_kwargs={'data': {str(album['id']): album for album in
                                         artist_albums + artist_singles + credit_albums}}
        )

    def get_album_info(self, album_id: str, data=None) -> AlbumInfo:
        # check if album is already in album cache, add it
        if data is None:
            data = {}

        album_data = data[album_id] if album_id in data else self.session.get_album(album_id)

        # get all album tracks with corresponding credits
        tracks_data = self.session.get_album_contributors(album_id)

        # add the track contributors to a new list called 'credits'
        cache = {'data': {}}
        for track in tracks_data['items']:
            track['item'].update({'credits': track['credits']})
            cache['data'][str(track['item']['id'])] = track['item']

        tracks = [str(track['item']['id']) for track in tracks_data['items']]

        if album_data['audioModes'] == ['DOLBY_ATMOS']:
            quality = 'Dolby Atmos'
        elif album_data['audioModes'] == ['SONY_360RA']:
            quality = '360'
        elif album_data['audioQuality'] == 'HI_RES':
            quality = 'M'
        else:
            quality = None

        return AlbumInfo(
            name=album_data['title'],
            release_year=album_data['releaseDate'][:4],
            explicit=album_data['explicit'],
            quality=quality,
            upc=album_data['upc'],
            all_track_cover_jpg_url=self.generate_artwork_url(album_data['cover'],
                                                              size=self.cover_size) if album_data['cover'] else None,
            animated_cover_url=self.generate_animated_artwork_url(album_data['videoCover']) if album_data[
                'videoCover'] else None,
            artist=album_data['artist']['name'],
            artist_id=album_data['artist']['id'],
            tracks=tracks,
            track_extra_kwargs=cache
        )

    def get_track_info(self, track_id: str, quality_tier: QualityEnum, codec_options: CodecOptions,
                       data=None) -> TrackInfo:
        if data is None:
            data = {}

        track_data = data[track_id] if track_id in data else self.session.get_track(track_id)

        album_id = str(track_data['album']['id'])
        # check if album is already in album cache, get it
        album_data = data[album_id] if album_id in data else self.session.get_album(album_id)

        # get Sony 360RA and switch to mobile session
        if (track_data['audioModes'] == ['SONY_360RA']
            or (track_data['audioModes'] == ['DOLBY_ATMOS'] and self.prefer_ac4)) \
                and SessionType.MOBILE.name in self.available_sessions:
            self.session.default = SessionType.MOBILE
        else:
            self.session.default = SessionType.TV

        stream_data = self.session.get_stream_url(track_id, self.quality_parse[quality_tier])
        # only needed for MPEG-DASH
        audio_track = None

        if stream_data['manifestMimeType'] == 'application/dash+xml':
            manifest = base64.b64decode(stream_data['manifest'])
            audio_track = self.parse_mpd(manifest)[0]  # Only one AudioTrack?
            track_codec = audio_track.codec
        else:
            manifest = json.loads(base64.b64decode(stream_data['manifest']))
            track_codec = CodecEnum['AAC' if 'mp4a' in manifest['codecs'] else manifest['codecs'].upper()]

        if not codec_data[track_codec].spatial:
            if not codec_options.proprietary_codecs and codec_data[track_codec].proprietary:
                self.print(f'Proprietary codecs are disabled, if you want to download {track_codec.name}, '
                           f'set "proprietary_codecs": true', drop_level=1)
                stream_data = self.session.get_stream_url(track_id, 'LOSSLESS')

                if stream_data['manifestMimeType'] == 'application/dash+xml':
                    manifest = base64.b64decode(stream_data['manifest'])
                    audio_track = self.parse_mpd(manifest)[0]  # Only one AudioTrack?
                    track_codec = audio_track.codec
                else:
                    manifest = json.loads(base64.b64decode(stream_data['manifest']))
                    track_codec = CodecEnum['AAC' if 'mp4a' in manifest['codecs'] else manifest['codecs'].upper()]

        track_name = track_data["title"]
        track_name += f' ({track_data["version"]})' if track_data['version'] else ''

        if audio_track:
            download_args = {'audio_track': audio_track}
        else:
            download_args = {'file_url': manifest['urls'][0]}

        track_info = TrackInfo(
            name=track_name,
            album=album_data['title'],
            album_id=album_id,
            artists=[a['name'] for a in track_data['artists']],
            artist_id=track_data['artist']['id'],
            release_year=track_data['streamStartDate'][:4],
            # TODO: Get correct bit_depth and sample_rate for MQA, even possible?
            bit_depth=24 if track_codec in [CodecEnum.MQA, CodecEnum.EAC3, CodecEnum.MHA1] else 16,
            sample_rate=48 if track_codec in [CodecEnum.EAC3, CodecEnum.MHA1, CodecEnum.AC4] else 44.1,
            cover_url=self.generate_artwork_url(track_data['album']['cover'], size=self.cover_size),
            explicit=track_data['explicit'] if 'explicit' in track_data else None,
            tags=self.convert_tags(track_data, album_data),
            codec=track_codec,
            download_extra_kwargs=download_args,
            lyrics_extra_kwargs={'track_data': track_data},
            # check if 'credits' are present (only from get_album_data)
            credits_extra_kwargs={'data': {track_id: track_data['credits']} if 'credits' in track_data else {}}
        )

        if not codec_options.spatial_codecs and codec_data[track_codec].spatial:
            track_info.error = 'Spatial codecs are disabled, if you want to download it, set "spatial_codecs": true'

        return track_info

    @staticmethod
    def parse_mpd(xml: bytes) -> list:
        xml = xml.decode('UTF-8')
        # Removes default namespace definition, don't do that!
        xml = re.sub(r'xmlns="[^"]+"', '', xml, count=1)
        root = ElementTree.fromstring(xml)

        # List of AudioTracks
        tracks = []

        for period in root.findall('Period'):
            for adaptation_set in period.findall('AdaptationSet'):
                for rep in adaptation_set.findall('Representation'):
                    # Check if representation is audio
                    content_type = adaptation_set.get('contentType')
                    if content_type != 'audio':
                        raise ValueError('Only supports audio MPDs!')

                    # Codec checks
                    codec = rep.get('codecs').upper()
                    if codec.startswith('MP4A'):
                        codec = 'AAC'

                    # Segment template
                    seg_template = rep.find('SegmentTemplate')
                    # Add init file to track_urls
                    track_urls = [seg_template.get('initialization')]
                    start_number = int(seg_template.get('startNumber') or 1)

                    # https://dashif-documents.azurewebsites.net/Guidelines-TimingModel/master/Guidelines-TimingModel.html#addressing-explicit
                    # Also see example 9
                    seg_timeline = seg_template.find('SegmentTimeline')
                    if seg_timeline is not None:
                        seg_time_list = []
                        cur_time = 0

                        for s in seg_timeline.findall('S'):
                            # Media segments start time
                            if s.get('t'):
                                cur_time = int(s.get('t'))

                            # Segment reference
                            for i in range((int(s.get('r') or 0) + 1)):
                                seg_time_list.append(cur_time)
                                # Add duration to current time
                                cur_time += int(s.get('d'))

                        # Create list with $Number$ indices
                        seg_num_list = list(range(start_number, len(seg_time_list) + start_number))
                        # Replace $Number$ with all the seg_num_list indices
                        track_urls += [seg_template.get('media').replace('$Number$', str(n)) for n in seg_num_list]

                    tracks.append(AudioTrack(
                        codec=CodecEnum[codec],
                        sample_rate=int(rep.get('audioSamplingRate') or 0),
                        bitrate=int(rep.get('bandwidth') or 0),
                        urls=track_urls
                    ))

        return tracks

    def get_track_download(self, file_url: str = None, audio_track: AudioTrack = None) -> TrackDownloadInfo:
        # no MPEG-DASH, just a simple file
        if file_url:
            return TrackDownloadInfo(download_type=DownloadEnum.URL, file_url=file_url)

        # MPEG-DASH
        # use the total_file size for a better progress bar? Is it even possible to calculate the total size from MPD?
        try:
            columns = os.get_terminal_size().columns
            if os.name == 'nt':
                bar = tqdm(audio_track.urls, ncols=(columns - self.oprinter.indent_number),
                           bar_format=' ' * self.oprinter.indent_number + '{l_bar}{bar}{r_bar}')
            else:
                raise OSError
        except OSError:
            bar = tqdm(audio_track.urls, bar_format=' ' * self.oprinter.indent_number + '{l_bar}{bar}{r_bar}')

        # download all segments and save the locations inside temp_locations
        temp_locations = []
        for download_url in bar:
            temp_locations.append(download_to_temp(download_url, extension='mp4'))

        # concatenated/Merged .mp4 file
        merged_temp_location = create_temp_filename() + '.mp4'
        # actual converted .flac file
        output_location = create_temp_filename() + '.' + codec_data[audio_track.codec].container.name

        # download is finished, merge chunks into 1 file
        with open(merged_temp_location, 'wb') as dest_file:
            for temp_location in temp_locations:
                with open(temp_location, 'rb') as segment_file:
                    copyfileobj(segment_file, dest_file)

        # convert .mp4 back to .flac
        try:
            ffmpeg.input(merged_temp_location, hide_banner=None, y=None).output(output_location, acodec='copy',
                                                                                loglevel='error').run()
            # Remove all files
            silentremove(merged_temp_location)
            for temp_location in temp_locations:
                silentremove(temp_location)
        except Exception:
            self.print('FFmpeg is not installed or working! Using fallback, may have errors')

            # return the MP4 temp file, but tell orpheus to change the container to .m4a (AAC)
            return TrackDownloadInfo(
                download_type=DownloadEnum.TEMP_FILE_PATH,
                temp_file_path=merged_temp_location,
                different_codec=CodecEnum.AAC
            )

        # return the converted flac file now
        return TrackDownloadInfo(
            download_type=DownloadEnum.TEMP_FILE_PATH,
            temp_file_path=output_location,
        )

    def get_track_cover(self, track_id: str, cover_options: CoverOptions, data=None) -> CoverInfo:
        if data is None:
            data = {}

        track_data = data[track_id] if track_id in data else self.session.get_track(track_id)
        cover_id = track_data['album']['cover']

        # Tidal don't support PNG, so it will always get JPG
        cover_url = self.generate_artwork_url(cover_id, size=cover_options.resolution)
        return CoverInfo(url=cover_url, file_type=ImageFileTypeEnum.jpg)

    def get_track_lyrics(self, track_id: str, track_data: dict) -> LyricsInfo:
        embedded, synced = None, None

        lyrics_data = self.session.get_lyrics(track_id)

        if 'error' in lyrics_data:
            # search for title and artist to find a matching track (non Atmos)
            results = self.search(
                DownloadTypeEnum.track,
                f'{track_data["title"]} {"".join(a["name"] for a in track_data["artists"])}',
                limit=10)

            # check every result to find a matching result
            best_tracks = [r.result_id for r in results
                           if r.name == track_data['title'] and
                           r.artists[0] == track_data['artist']['name'] and
                           'Dolby Atmos' not in r.additional]

            # retrieve the lyrics for the first one, otherwise return empty dict
            lyrics_data = self.session.get_lyrics(best_tracks[0]) if len(best_tracks) > 0 else {}

        if 'lyrics' in lyrics_data:
            embedded = lyrics_data['lyrics']

        if 'subtitles' in lyrics_data:
            synced = lyrics_data['subtitles']

        return LyricsInfo(
            embedded=embedded,
            synced=synced
        )

    def get_track_credits(self, track_id: str, data=None) -> Optional[list]:
        if data is None:
            data = {}

        credits_dict = {}

        # fetch credits from cache if not fetch those credits
        if track_id in data:
            track_contributors = data[track_id]

            for contributor in track_contributors:
                credits_dict[contributor['type']] = [c['name'] for c in contributor['contributors']]
        else:
            track_contributors = self.session.get_track_contributors(track_id)['items']

            if len(track_contributors) > 0:
                for contributor in track_contributors:
                    # check if the dict contains no list, create one
                    if contributor['role'] not in credits_dict:
                        credits_dict[contributor['role']] = []

                    credits_dict[contributor['role']].append(contributor['name'])

        if len(credits_dict) > 0:
            # convert the dictionary back to a list of CreditsInfo
            return [CreditsInfo(sanitise_name(k), v) for k, v in credits_dict.items()]
        return None

    @staticmethod
    def convert_tags(track_data: dict, album_data: dict) -> Tags:
        track_name = track_data["title"]
        track_name += f' ({track_data["version"]})' if track_data['version'] else ''

        return Tags(
            album_artist=album_data['artist']['name'],
            track_number=track_data['trackNumber'],
            total_tracks=album_data['numberOfTracks'],
            disc_number=track_data['volumeNumber'],
            total_discs=album_data['numberOfVolumes'],
            isrc=track_data['isrc'],
            upc=album_data['upc'],
            release_date=album_data['releaseDate'] if 'releaseDate' in album_data else None,
            copyright=track_data['copyright'],
            replay_gain=track_data['replayGain'],
            replay_peak=track_data['peak']
        )
