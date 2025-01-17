"""The Chime TTS integration."""

import logging
import os
import io
from datetime import datetime

from pydub import AudioSegment

from homeassistant.components.media_player.const import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_ANNOUNCE,
    ATTR_MEDIA_VOLUME_LEVEL,
    ATTR_GROUP_MEMBERS,
    SERVICE_PLAY_MEDIA,
    SERVICE_JOIN,
    SERVICE_UNJOIN,
    MEDIA_TYPE_MUSIC,
)
from homeassistant.const import CONF_ENTITY_ID, SERVICE_VOLUME_SET
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceResponse, SupportsResponse
from homeassistant.helpers import storage
from homeassistant.helpers.network import get_url
from homeassistant.components import tts
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    TemplateError,
)

from .config_flow import ChimeTTSOptionsFlowHandler
from .helpers import ChimeTTSHelper
from .queue_manager import ChimeTTSQueueManager

from .const import (
    DOMAIN,
    SERVICE_SAY,
    SERVICE_SAY_URL,
    SERVICE_CLEAR_CACHE,
    VERSION,
    DATA_STORAGE_KEY,
    AUDIO_PATH_KEY,
    AUDIO_DURATION_KEY,
    ROOT_PATH_KEY,
    DEFAULT_TEMP_CHIMES_PATH_KEY,
    TEMP_CHIMES_PATH_KEY,
    TEMP_CHIMES_PATH_DEFAULT,
    DEFAULT_TEMP_PATH_KEY,
    TEMP_PATH_KEY,
    TEMP_PATH_DEFAULT,
    DEFAULT_WWW_PATH_KEY,
    WWW_PATH_KEY,
    WWW_PATH_DEFAULT,
    MEDIA_DIR_KEY,
    MEDIA_DIR_DEFAULT,
    MP3_PRESET_CUSTOM_PREFIX,
    MP3_PRESET_CUSTOM_KEY,
    QUEUE_TIMEOUT_KEY,
    QUEUE_TIMEOUT_DEFAULT,
    AMAZON_POLLY,
    BAIDU,
    GOOGLE_CLOUD,
    GOOGLE_TRANSLATE,
    IBM_WATSON_TTS,
    MARYTTS,
    MICROSOFT_EDGE_TTS,
    MICROSOFT_TTS,
    NABU_CASA_CLOUD_TTS,
    NABU_CASA_CLOUD_TTS_OLD,
    OPENAI_TTS,
    PICOTTS,
    PIPER,
    VOICE_RSS,
    YANDEX_TTS,
)

_LOGGER = logging.getLogger(__name__)
_data = {}

helpers = ChimeTTSHelper()
queue = ChimeTTSQueueManager()


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up an entry."""
    await async_refresh_stored_data(hass)
    update_configuration(config_entry, hass)
    queue.set_timeout(_data[QUEUE_TIMEOUT_KEY])

    config_entry.async_on_unload(config_entry.add_update_listener(async_reload_entry))
    return True


async def async_setup(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up the Chime TTS integration."""
    _LOGGER.info("The Chime TTS integration is set up.")

    ###############
    # Say Service #
    ###############

    async def async_say(service, is_say_url = False):
        if is_say_url is False:
            _LOGGER.debug("----- Chime TTS Say Called. Version %s -----", VERSION)

        # Add service calls to the queue with arguments
        result = await queue.add_to_queue(async_say_execute,service)

        if result is not False:
            return result

        # Service call failed
        return {}


    async def async_say_execute(service):
        """Play TTS audio with local chime MP3 audio."""
        start_time = datetime.now()

        # Parse service parameters & TTS options
        params = await helpers.async_parse_params(service.data, hass)
        options = helpers.parse_options_yaml(service.data)

        media_players_array = params["media_players_array"]

        # Create audio file to play on media player
        audio_dict = await async_get_playback_audio_path(params, options)
        if audio_dict is None or audio_dict[AUDIO_PATH_KEY] is None:
            return False
        _LOGGER.debug(" - audio_dict = %s", str(audio_dict))
        audio_path = audio_dict[AUDIO_PATH_KEY]
        audio_duration = audio_dict[AUDIO_DURATION_KEY]

        # Play audio with service_data
        if media_players_array is not False:
            play_result = await async_play_media(
                hass,
                audio_path,
                params["entity_ids"],
                params["announce"],
                params["join_players"],
                media_players_array,
                params["volume_level"],
            )
            if play_result is True:
                await async_post_playback_actions(
                    hass,
                    audio_duration,
                    params["final_delay"],
                    media_players_array,
                    params["volume_level"],
                    params["unjoin_players"],
                )

        # Save generated temp mp3 file to cache
        if params["cache"] is True or params["entity_ids"] is None or len(params["entity_ids"])==0:
            if _data["is_save_generated"] is True:
                if params["cache"]:
                    _LOGGER.debug("Saving generated mp3 file to cache")
                filepath_hash = _data["generated_filename"]
                await async_store_data(hass, filepath_hash, audio_dict)
        else:
            if os.path.exists(audio_path):
                os.remove(audio_path)

        end_time = datetime.now()
        elapsed_time = (end_time - start_time).total_seconds() * 1000

        # Convert URL to external for chime_tts.say_url
        if params["entity_ids"] is None or len(params["entity_ids"]) == 0:
            instance_url = hass.config.external_url
            if instance_url is None:
                instance_url = str(get_url(hass))

            external_url = (
                (instance_url + "/" + audio_path)
                .replace(instance_url + "//", instance_url + "/")
                .replace("/config", "")
                .replace("www/", "local/")
            )
            _LOGGER.debug("Final URL = %s", external_url)

            _LOGGER.debug("----- Chime TTS Say URL Completed in %s ms -----", str(elapsed_time))

            return {
                "url": external_url,
                "duration": audio_duration
            }

        _LOGGER.debug("----- Chime TTS Say Completed in %s ms -----", str(elapsed_time))


    hass.services.async_register(DOMAIN, SERVICE_SAY, async_say)

    ###################
    # Say URL Service #
    ###################

    async def async_say_url(service) -> ServiceResponse:
        """Create a public URL to an audio file generated with the `chime_tts.say` service."""
        _LOGGER.debug("----- Chime TTS Say URL Called. Version %s -----", VERSION)
        return await async_say(service, True)

    hass.services.async_register(DOMAIN,
                                 SERVICE_SAY_URL,
                                 async_say_url,
                                 supports_response=SupportsResponse.ONLY)

    #######################
    # Clear Cahce Service #
    #######################

    async def async_clear_cache(service):
        """Play TTS audio with local chime MP3 audio."""
        _LOGGER.debug("----- Chime TTS Clear Cache Called -----")
        clear_chimes_cache = bool(service.data.get("clear_chimes_cache", False))
        clear_temp_tts_cache = bool(service.data.get("clear_temp_tts_cache", False))
        clear_www_tts_cache = bool(service.data.get("clear_www_tts_cache", False))
        clear_ha_tts_cache = bool(service.data.get("clear_ha_tts_cache", False))

        start_time = datetime.now()

        to_log = []
        if clear_chimes_cache:
            to_log.append("cached downloaded chimes")
        if clear_temp_tts_cache is True:
            to_log.append("cached temporary Chime TTS audio files")
        if clear_www_tts_cache:
            to_log.append("cached publicly accessible Chime TTS audio files")
        if len(to_log) > 0:
            log_message = "Clearing "
            for i in range(len(to_log)):
                elem = to_log[i]
                if i == len(to_log)-1:
                    log_message += " and "
                elif i > 0:
                    log_message += ", "
                log_message += elem
            log_message += "..."
            _LOGGER.debug("%s", log_message)
        else:
            return


        # CLEAR CHIME TTS CACHE #
        cached_dicts = dict(_data[DATA_STORAGE_KEY])
        for key in cached_dicts:
            await async_remove_cached_audio_data(hass,
                                                 str(key),
                                                 clear_chimes_cache,
                                                 clear_temp_tts_cache,
                                                 clear_www_tts_cache)

        # CLEAR HA TTS CACHE #
        if clear_ha_tts_cache:
            _LOGGER.debug("Clearing cached Home Assistant TTS audio files...")
            await hass.services.async_call(domain="TTS",
                                           service="clear_cache",
                                           blocking=True)

        # Summary
        elapsed_time = (datetime.now() - start_time).total_seconds() * 1000
        _LOGGER.debug(
            "----- Chime TTS Clear Cache Completed in %s ms -----", str(elapsed_time)
        )

        return True

    hass.services.async_register(DOMAIN, SERVICE_CLEAR_CACHE, async_clear_cache)

    return True


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    return True


async def async_reload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Reload the Chime TTS config entry."""
    _LOGGER.debug("Reloading integration")
    await async_unload_entry(hass, config_entry)
    await async_setup_entry(hass, config_entry)
    await async_setup(hass, config_entry)


async def async_post_playback_actions(
    hass: HomeAssistant,
    audio_duration: float,
    final_delay: float,
    media_players_array: list,
    volume_level: float,
    unjoin_players: bool,
):
    """Run post playback actions."""
    # Wait the audio playback duration
    _LOGGER.debug("Waiting %ss for audio playback to complete...", str(audio_duration))
    await hass.async_add_executor_job(helpers.sleep, audio_duration)
    if final_delay > 0:
        final_delay_s = float(final_delay / 1000)
        _LOGGER.debug("Waiting %ss for final_delay to complete...", str(final_delay_s))
        await hass.async_add_executor_job(helpers.sleep, final_delay_s)

    # Reset media players back to their original states
    entity_ids = []

    # Reset volume
    for media_player_dict in media_players_array:
        entity_id = media_player_dict["entity_id"]
        entity_ids.append(entity_id)
        should_change_volume = bool(media_player_dict["should_change_volume"])
        initial_volume_level = media_player_dict["initial_volume_level"]
        if should_change_volume and initial_volume_level >= 0:
            _LOGGER.debug(
                "Returning %s's volume level to %s", entity_id, initial_volume_level
            )
            await async_set_volume_level(
                hass, entity_id, initial_volume_level, volume_level
            )

    # Unjoin entity_ids
    if (
        unjoin_players is True
        and "joint_media_player_entity_id" in _data
        and _data["joint_media_player_entity_id"] is not None
    ):
        _LOGGER.debug(" - Calling media_player.unjoin service...")
        for media_player_dict in media_players_array:
            if media_player_dict["group_members_supported"] is True:
                entity_id = media_player_dict["entity_id"]
                _LOGGER.debug("   - media_player.unjoin: %s", entity_id)
                try:
                    await hass.services.async_call(
                        domain="media_player",
                        service=SERVICE_UNJOIN,
                        service_data={CONF_ENTITY_ID: entity_id},
                        blocking=True,
                    )
                    _LOGGER.debug("   ...done")
                except Exception as error:
                    _LOGGER.warning(
                        " - Error calling unjoin service for %s: %s", entity_id, error
                    )


async def async_join_media_players(hass, entity_ids):
    """Join media players."""
    _LOGGER.debug(
        " - Calling media_player.join service for %s media_player entities...",
        len(entity_ids),
    )

    supported_entity_ids = []
    for entity_id in entity_ids:
        entity = hass.states.get(entity_id)
        if helpers.get_supported_feature(entity, ATTR_GROUP_MEMBERS):
            supported_entity_ids.append(entity_id)

    if len(supported_entity_ids) > 1:
        _LOGGER.debug(
            " - Joining %s media_player entities...", str(len(supported_entity_ids))
        )
        try:
            _data["joint_media_player_entity_id"] = supported_entity_ids[0]
            await hass.services.async_call(
                domain="media_player",
                service=SERVICE_JOIN,
                service_data={
                    CONF_ENTITY_ID: _data["joint_media_player_entity_id"],
                    ATTR_GROUP_MEMBERS: supported_entity_ids,
                },
                blocking=True,
            )
            _LOGGER.debug(" - ...done")
            return _data["joint_media_player_entity_id"]
        except Exception as error:
            _LOGGER.warning("   - Error joining media_player entities: %s", error)
    else:
        _LOGGER.warning(" - Only 1 media_player entity provided. Unable to join.")

    return False


####################################
### Retrieve TTS Audio Functions ###
####################################

async def async_request_tts_audio(
    hass: HomeAssistant,
    tts_platform: str,
    message: str,
    language: str,
    cache: bool,
    options: dict,
    tts_playback_speed: float = 0.0,
):
    """Send an API request for TTS audio and return the audio file's local filepath."""

    start_time = datetime.now()

    # Data validation

    tts_options = options.copy() if isinstance(options, dict) else (str(options) if isinstance(options, str) else options)

    if message is False or message == "":
        _LOGGER.warning("No message text provided for TTS audio")
        return None

    if tts_platform is False or tts_platform == "":
        _LOGGER.warning("No TTS platform selected")
        return None
    if tts_platform == NABU_CASA_CLOUD_TTS_OLD:
        tts_platform = NABU_CASA_CLOUD_TTS

    # Add & validate additional parameters

    # Language
    if language is not None and tts_platform in [
        GOOGLE_TRANSLATE,
        NABU_CASA_CLOUD_TTS,
        IBM_WATSON_TTS,
        MICROSOFT_EDGE_TTS,
    ]:
        if tts_platform is IBM_WATSON_TTS:
            tts_options["voice"] = language
    else:
        language = None

    # Cache
    use_cache = True if cache is True and tts_platform not in [GOOGLE_TRANSLATE, NABU_CASA_CLOUD_TTS] else False

    # tld
    if "tld" in tts_options and tts_platform not in [GOOGLE_TRANSLATE]:
        del tts_options["tld"]

    # Gender
    if "gender" in tts_options and tts_platform not in [NABU_CASA_CLOUD_TTS]:
        del tts_options["gender"]

    _LOGGER.debug("async_request_tts_audio(%s)",
        "tts_platform='" + tts_platform
        + "', message='" + str(message)
        + "', tts_playback_speed=" + str(tts_playback_speed)
        + ", cache=" + str(use_cache)
        + ", language=" + ("'" + str(language) + "'" if language is not None else "None")
        + ", options=" + str(tts_options)
    )
    _LOGGER.debug(" - Generating TTS audio...")
    media_source_id = None
    try:
        media_source_id = tts.media_source.generate_media_source_id(
            hass=hass,
            message=message,
            engine=tts_platform,
            language=language,
            cache=cache,
            options=tts_options,
        )
    except Exception as error:
        if f"{error}" == "Invalid TTS provider selected":
            missing_tts_platform_error(tts_platform)
        else:
            _LOGGER.error(
                "   - Error calling tts.media_source.generate_media_source_id: %s",
                error,
            )
        return None
    if media_source_id is None:
        _LOGGER.error(" - Error: Unable to generate media_source_id")
        return None

    audio_data = None
    try:
        audio_data = await tts.async_get_media_source_audio(
            hass=hass, media_source_id=media_source_id
        )
    except Exception as error:
        _LOGGER.error(
            "   - Error calling tts.async_get_media_source_audio: %s", error
        )
        return None

    if audio_data is not None:
        if len(audio_data) == 2:
            audio_bytes = audio_data[1]
            file = io.BytesIO(audio_bytes)
            if file is None:
                _LOGGER.error(" - ...could not convert TTS bytes to audio")
                return None
            audio = AudioSegment.from_file(file)
            if audio is not None:
                if tts_playback_speed != 100:
                    _LOGGER.debug(
                        " -  ...changing TTS playback speed to %s percent",
                        str(tts_playback_speed),
                    )
                    playback_speed = float(tts_playback_speed / 100)
                    if tts_playback_speed > 150:
                        audio = audio.speedup(
                            playback_speed=playback_speed, chunk_size=50
                        )
                    else:
                        audio = audio.speedup(playback_speed=playback_speed)
                end_time = datetime.now()
                _LOGGER.debug(
                    " - ...TTS audio completed in %s ms",
                    str((end_time - start_time).total_seconds() * 1000),
                )
                return audio
            _LOGGER.error(" - ...could not extract TTS audio from file")
        else:
            _LOGGER.error(" - ...audio_data did not contain audio bytes")
    else:
        _LOGGER.error(" - ...audio_data generation failed")
    return None


def missing_tts_platform_error(tts_platform):
    """Write a TTS platform specific debug warning when the TTS platform has not been configured."""
    tts_platform_name = tts_platform
    if tts_platform is AMAZON_POLLY:
        tts_platform_name = "Amazon Polly"
    if tts_platform is BAIDU:
        tts_platform_name = "Baidu"
    if tts_platform is GOOGLE_CLOUD:
        tts_platform_name = "Google Cloud"
    if tts_platform is GOOGLE_TRANSLATE:
        tts_platform_name = "Google Translate"
    if tts_platform is IBM_WATSON_TTS:
        tts_platform_name = "Watson TTS"
    if tts_platform is MARYTTS:
        tts_platform_name = "MaryTTS"
    if tts_platform is MICROSOFT_TTS:
        tts_platform_name = "Microsoft TTS"
    if tts_platform is MICROSOFT_EDGE_TTS:
        tts_platform_name = "Microsoft Edge TTS"
    if tts_platform is NABU_CASA_CLOUD_TTS:
        tts_platform_name = "Nabu Casa Cloud TTS"
    if tts_platform is NABU_CASA_CLOUD_TTS_OLD:
        tts_platform_name = "Nabu Casa Cloud TTS"
    if tts_platform is OPENAI_TTS:
        tts_platform_name = "OpenAI TTS"
    if tts_platform is PICOTTS:
        tts_platform_name = "PicoTTS"
    if tts_platform is PIPER:
        tts_platform_name = "Piper"
    if tts_platform is VOICE_RSS:
        tts_platform_name = "VoiceRSS"
    if tts_platform is YANDEX_TTS:
        tts_platform_name = "Yandex TTS"
    _LOGGER.error(
        "The %s platform was not found. Please check that it has been configured correctly: https://www.home-assistant.io/integrations/#text-to-speech",
        tts_platform_name,
    )


##############################
### Audio Helper Functions ###
##############################


async def async_get_playback_audio_path(params: dict, options: dict):
    """Create audio to play on media player entity."""
    output_audio = None

    hass = params["hass"]
    chime_path = params["chime_path"]
    end_chime_path = params["end_chime_path"]
    offset = params["offset"]
    message = params["message"]
    cache = params["cache"]
    entity_ids = params["entity_ids"]
    ffmpeg_args = params["ffmpeg_args"]
    _data["is_save_generated"] = False
    _LOGGER.debug("async_get_playback_audio_path")

    filepath_hash = get_filename_hash_from_service_data({**params}, {**options})
    _data["generated_filename"] = filepath_hash

    # Load previously generated audio from cache
    if cache is True:
        _LOGGER.debug("Attempting to retrieve generated mp3 file from cache")
        audio_dict = await async_get_cached_audio_data(hass, filepath_hash)
        if audio_dict is not None:
            filepath = audio_dict[AUDIO_PATH_KEY]
            audio_duration = audio_dict[AUDIO_DURATION_KEY]
            if filepath is not None and audio_duration > 0:
                if os.path.exists(str(filepath)):
                    _LOGGER.debug("Using previously generated mp3 saved in cache")
                    return audio_dict
                _LOGGER.warning("Could not find previosuly cached generated mp3 file")
        else:
            _LOGGER.debug(" - No previously generated mp3 file found")

    ######################
    # Generate new audio #
    ######################

    # Load chime audio
    output_audio = await async_get_audio_from_path(hass=hass,
                                                    filepath=chime_path,
                                                    cache=cache)

    # Process message tags
    output_audio = await async_process_segments(hass,
                                                message,
                                                output_audio,
                                                params,
                                                options)

    # Load end chime audio
    output_audio = await async_get_audio_from_path(hass=hass,
                                                   filepath=end_chime_path,
                                                   cache=cache,
                                                   offset=offset,
                                                   audio=output_audio)

    # Save generated audio file
    if output_audio is not None:
        duration = float(len(output_audio) / 1000.0)
        _LOGGER.debug(" - Final audio created. Duration: %ss", duration)

        # Save MP3 file
        _LOGGER.debug(" - Saving mp3 file...")
        if entity_ids and len(entity_ids) > 0:
            # Use the temp folder path
            new_audio_folder = _data[TEMP_PATH_KEY]
        else:
            # Use the public folder path (i.e chime_tts.say_url service calls)
            new_audio_folder = _data[WWW_PATH_KEY]

        new_audio_full_path = helpers.save_audio_to_folder(output_audio, new_audio_folder)

        # Perform FFmpeg conversion
        if ffmpeg_args:
            _LOGGER.debug("  - Performing FFmpeg audio conversion...")
            converted_output_audio = helpers.ffmpeg_convert_from_file(new_audio_full_path,
                                                                      ffmpeg_args)
            if converted_output_audio is not False:
                _LOGGER.debug("  - ...FFmpeg audio conversion completed.")
                new_audio_full_path = converted_output_audio
            else:
                _LOGGER.warning("  - ...FFmpeg audio conversion failed. Using unconverted audio file")

        _LOGGER.debug("  - Filepath = '%s'", new_audio_full_path)
        _data["is_save_generated"] = True


        # Check URL (chime_tts.say_url)
        if entity_ids is None or len(entity_ids) == 0:
            relative_path = new_audio_full_path
            new_audio_full_path = helpers.validate_path(hass, new_audio_full_path)
            if relative_path != new_audio_full_path:
                _LOGGER.debug("  - Non-relative filepath = '%s'", new_audio_full_path)

        if new_audio_full_path is None:
            _LOGGER.error("Unable to save audio file.")
            return None
        _LOGGER.debug("  - File saved successfully")

        # Valdiation
        audio_dict = {AUDIO_PATH_KEY: new_audio_full_path, AUDIO_DURATION_KEY: duration}
        if audio_dict[AUDIO_DURATION_KEY] == 0:
            _LOGGER.error("async_get_playback_audio_path --> Audio has no duration")
            audio_dict = None
        if audio_dict[AUDIO_DURATION_KEY] == 0:
            _LOGGER.error("async_get_playback_audio_path --> Audio has no duration")
            audio_dict = None
        if audio_dict[AUDIO_PATH_KEY] is not None and len(audio_dict[AUDIO_PATH_KEY]) == 0:
            _LOGGER.error(
                "async_get_playback_audio_path --> Audio has no file path data"
            )
            audio_dict = None

        return audio_dict

    return None


def get_segment_offset(output_audio, segment, params):
    """Offset value for segment."""
    segment_offset = 0
    if output_audio is not None:
        # Get "offset" parameter
        if "offset" in segment:
            segment_offset = segment["offset"]

        # Support deprecated "delay" parmeter
        else:
            if "delay" in segment:
                segment_offset = segment["delay"]
            elif "delay" in params:
                segment_offset = params["delay"]
            elif "offset" in params:
                segment_offset = params["offset"]

    return segment_offset


async def async_process_segments(hass, message, output_audio, params, options):
    """Process all message segments and add the audio."""
    segments = helpers.parse_message(message)
    if segments is None or len(segments) == 0:
        return output_audio

    for index, segment in enumerate(segments):
        segment_cache = segment["cache"] if "cache" in segment else params["cache"]
        segment_audio_conversion = segment["audio_conversion"] if "audio_conversion" in segment else None
        segment_offset = get_segment_offset(output_audio, segment, params)

        # Chime tag
        if segment["type"] == "chime":
            if "path" in segment:
                output_audio = await async_get_audio_from_path(hass=hass,
                                                               filepath=segment["path"],
                                                               cache=segment_cache,
                                                               offset=segment_offset,
                                                               audio=output_audio)
            else:
                _LOGGER.warning("Chime path missing from messsage segment #%s", str(index+1))

        # Delay tag
        if segment["type"] == "delay":
            if "length" in segment:
                segment_delay_length = float(segment["length"])
                output_audio = output_audio + AudioSegment.silent(duration=segment_delay_length)
            else:
                _LOGGER.warning("Delay length missing from messsage segment #%s", str(index+1))

        # Request TTS audio file
        if segment["type"] == "tts":
            if "message" in segment and len(segment["message"]) > 0:
                segment_message = segment["message"]
                if len(segment_message) == 0 or segment_message == "None":
                    continue

                segment_tts_platform = segment["tts_platform"] if "tts_platform" in segment else params["tts_platform"]
                segment_language = segment["language"] if "language" in segment else params["language"]
                segment_tts_playback_speed = segment["tts_playback_speed"] if "tts_playback_speed" in segment else params["tts_playback_speed"]

                # Use exposed parameters if not present in the options dictionary
                segment_options = segment["options"] if "options" in segment else {}
                exposed_option_keys = ["gender", "tld", "voice"]
                for exposed_option_key in exposed_option_keys:
                    value = None
                    if exposed_option_key in segment_options:
                        value = segment_options[exposed_option_key]
                    elif exposed_option_key in segment:
                        value = segment[exposed_option_key]
                    if value is not None:
                        segment_options[exposed_option_key] = value

                for key, value in options.items():
                    if key not in segment_options:
                        segment_options[key] = value
                segment_params = {
                    "message": segment_message,
                    "tts_platform": segment_tts_platform,
                    "language": segment_language,
                    "cache": segment_cache,
                    "tts_playback_speed": segment_tts_playback_speed
                }
                segment_filepath_hash = get_filename_hash_from_service_data({**segment_params}, {**segment_options}, )

                tts_audio = None

                # Use cached TTS audio
                if segment_cache is True:
                    _LOGGER.debug(" - Attempting to retrieve TTS file from cache...")
                    audio_dict = await async_get_cached_audio_data(hass, segment_filepath_hash)
                    if audio_dict is not None:
                        tts_audio = await async_get_audio_from_path(hass=hass,
                                                                    filepath=audio_dict[AUDIO_PATH_KEY],
                                                                    cache=segment_cache,
                                                                    audio=None)

                        tts_audio_duration = audio_dict[AUDIO_DURATION_KEY]
                        _LOGGER.debug(" - ...cached TTS file retrieved with duration: %ss", str(tts_audio_duration))
                    else:
                        _LOGGER.debug(" - ...cached TTS file not found")


                # Generate new TTS audio
                if tts_audio is None:
                    tts_audio = await async_request_tts_audio(
                        hass=hass,
                        tts_platform=segment_tts_platform,
                        message=segment_message,
                        language=segment_language,
                        cache=segment_cache,
                        options=segment_options,
                        tts_playback_speed=segment_tts_playback_speed,
                    )


                # Combine audio
                if tts_audio is not None:
                    tts_audio_duration = float(len(tts_audio) / 1000.0)
                    output_audio = helpers.combine_audio(output_audio, tts_audio, segment_offset)

                    # Cache the new TTS audio?
                    if segment_cache is True and audio_dict is None:
                        _LOGGER.debug("Saving generated TTS audio to cache")
                        tts_audio_full_path = helpers.save_audio_to_folder(
                            tts_audio, _data[TEMP_PATH_KEY])
                        if tts_audio_full_path is not None:
                            audio_dict = {
                                AUDIO_PATH_KEY: tts_audio_full_path,
                                AUDIO_DURATION_KEY: tts_audio_duration
                            }
                            await async_store_data(hass, segment_filepath_hash, audio_dict)

                        else:
                            _LOGGER.warning("Unable to save generated TTS audio to cache")

                else:
                    _LOGGER.warning("Error generating TTS audio from messsage segment #%s: %s",
                                    str(index+1), str(segment))
            else:
                _LOGGER.warning("TTS message missing from messsage segment #%s: %s",
                                str(index+1), str(segment))

        # Audio Conversion with FFmpeg
        if segment_audio_conversion is not None:
            _LOGGER.debug("Converting audio segment with FFmpeg...")
            temp_folder = _data[TEMP_PATH_KEY]
            output_audio = helpers.ffmpeg_convert_from_audio_segment(output_audio, segment_audio_conversion, temp_folder)

    return output_audio

async def async_get_audio_from_path(hass: HomeAssistant,
                                    filepath: str,
                                    cache=False,
                                    offset=0,
                                    audio=None):
    """Add audio from a given file path to existing audio (optional) with offset (optional)."""
    if filepath is None or filepath == "None" or len(filepath) == 0:
        return audio

    # Load/download audio file & validate local path
    # await async_refresh_stored_data(hass)
    filepath = await helpers.async_get_chime_path(
        chime_path=filepath,
        cache=cache,
        data=_data,
        hass=hass)

    if filepath is not None:
        _LOGGER.debug('Retrieving audio from path: "%s"', filepath)
        try:
            audio_from_path = AudioSegment.from_file(filepath)
            if audio_from_path is not None:
                duration = float(len(audio_from_path) / 1000.0)
                _LOGGER.debug(
                    " - Audio retrieved. Duration: %ss",
                    str(duration),
                )
                if audio is None:
                    return audio_from_path

                # Apply offset
                return helpers.combine_audio(audio, audio_from_path, offset)
            _LOGGER.warning("Unable to find audio at filepath: %s", filepath)
        except Exception as error:
            _LOGGER.warning('Unable to extract audio from file: "%s"', error)
    return audio


async def async_set_volume_level_for_media_players(
    hass: HomeAssistant, media_players_array, volume_level: float
):
    """Set the volume level for all media_players."""
    for media_player_dict in media_players_array:
        entity_id = media_player_dict["entity_id"]
        should_change_volume = bool(media_player_dict["should_change_volume"])
        initial_volume_level = media_player_dict["initial_volume_level"]
        if should_change_volume and volume_level >= 0:
            _LOGGER.debug(
                " - Setting '%s' volume level to %s", entity_id, str(volume_level)
            )
            await async_set_volume_level(
                hass, entity_id, volume_level, initial_volume_level
            )


async def async_set_volume_level(
    hass: HomeAssistant, entity_id: str, new_volume_level=-1, current_volume_level=-1
):
    """Set the volume_level for a given media player entity."""
    new_volume_level = float(new_volume_level)
    current_volume_level = float(current_volume_level)
    _LOGGER.debug(
        ' - async_set_volume_level("%s", %s)', entity_id, str(new_volume_level)
    )
    if new_volume_level >= 0 and new_volume_level != current_volume_level:
        _LOGGER.debug(
            ' - Seting volume_level of media player "%s" to: %s',
            entity_id,
            str(new_volume_level),
        )
        try:
            await hass.services.async_call(
                "media_player",
                SERVICE_VOLUME_SET,
                {ATTR_MEDIA_VOLUME_LEVEL: new_volume_level, CONF_ENTITY_ID: entity_id},
                True,
            )
            _LOGGER.debug(" - Volume set")
        except Exception as error:
            _LOGGER.warning(" - Error setting volume for '%s': %s", entity_id, error)
        return True
    _LOGGER.debug(" - Skipped setting volume")
    return False


async def async_play_media(
    hass: HomeAssistant,
    audio_path,
    entity_ids,
    announce,
    join_players,
    media_players_array,
    volume_level,
):
    """Call the media_player.play_media service."""
    service_data = {}

    # media content type
    service_data[ATTR_MEDIA_CONTENT_TYPE] = MEDIA_TYPE_MUSIC

    # media_content_id
    media_source_path = audio_path
    media_folder = "/media/"
    media_folder_path_index = media_source_path.find(media_folder)
    if media_folder_path_index != -1:
        media_path = media_source_path[media_folder_path_index + len(media_folder) :].replace("//", "/")
        media_source_path = "media-source://media_source/<media_dir>/<media_path>".replace(
            "<media_dir>", _data[MEDIA_DIR_KEY]
        ).replace(
            "<media_path>", media_path)
    service_data[ATTR_MEDIA_CONTENT_ID] = media_source_path

    # announce
    if announce is True:
        service_data[ATTR_MEDIA_ANNOUNCE] = announce

    # entity_id
    service_data[CONF_ENTITY_ID] = entity_ids
    if join_players is True:
        # join entity_ids as a group
        group_members_suppored = helpers.get_group_members_suppored(media_players_array)
        if group_members_suppored > 1:
            joint_speakers_entity_id = await async_join_media_players(hass, entity_ids)
            if joint_speakers_entity_id is not False:
                service_data[CONF_ENTITY_ID] = joint_speakers_entity_id
            else:
                _LOGGER.warning(
                    "Unable to join speakers. Only 1 media_player supported."
                )
        else:
            if group_members_suppored == 1:
                _LOGGER.warning(
                    "Unable to join speakers. Only 1 media_player supported."
                )
            else:
                _LOGGER.warning(
                    "Unable to join speakers. No supported media_players found."
                )

    # Set volume to desired level
    await async_set_volume_level_for_media_players(
        hass, media_players_array, volume_level
    )

    # Play the audio
    _LOGGER.debug("Calling media_player.play_media service with data:")
    for key, value in service_data.items():
        _LOGGER.debug(" - %s: %s", str(key), str(value))
    retry_count = 3
    should_retry = False
    for i in range(retry_count):
        if i == 0 or should_retry is True:
            should_retry = False
            if i > 0:
                _LOGGER.warning("...playback retry %s/%s", str(i+1), str(retry_count))
            try:
                await hass.services.async_call(
                    "media_player",
                    SERVICE_PLAY_MEDIA,
                    service_data,
                    True,
                )
                _LOGGER.debug("...media_player.play_media completed.")
                return True
            except ServiceNotFound:
                _LOGGER.error("Service 'play_media' not found.")
            except TemplateError as err:
                _LOGGER.error("Error while rendering Jinja2 template for audio playback: %s", err)
            except HomeAssistantError as err:
                _LOGGER.error("An error occurred: %s", str(err))
                if err == "Unknown source directory":
                    _LOGGER.warning(
                        "Please check that media directories are enabled in your configuration.yaml file, e.g:\r\n\r\nmedia_source:\r\n media_dirs:\r\n   local: /media"
                    )
            except Exception as err:
                _LOGGER.error("An unexpected error occurred when playing the audio: %s", str(err))
                should_retry = True

    return False


################################
### Storage Helper Functions ###
################################

async def async_refresh_stored_data(hass: HomeAssistant):
    """Refresh the stored data of the integration."""
    store = storage.Store(hass, 1, DATA_STORAGE_KEY)
    _data[DATA_STORAGE_KEY] = await store.async_load()
    if _data[DATA_STORAGE_KEY] is None:
        _data[DATA_STORAGE_KEY] = {}


async def async_store_data(hass: HomeAssistant, key: str, value: str):
    """Store a key/value pair in the integration's stored data."""
    _LOGGER.debug("Saving to chime_tts storage:")
    _LOGGER.debug(' - key:   "%s"', key)
    _LOGGER.debug(' - value: "%s"', value)
    _data[DATA_STORAGE_KEY][key] = value
    await async_save_data(hass)


async def async_retrieve_data(key: str):
    """Retrieve a value from the integration's stored data based on the provided key."""
    if key in _data[DATA_STORAGE_KEY]:
        return _data[DATA_STORAGE_KEY][key]
    return None


async def async_save_data(hass: HomeAssistant):
    """Save the provided data to the integration's stored data."""
    store = storage.Store(hass, 1, DATA_STORAGE_KEY)
    await store.async_save(_data[DATA_STORAGE_KEY])


async def async_get_cached_audio_data(hass: HomeAssistant, filepath_hash: str):
    """Return cached audio data previously stored in Chime TTS' cache."""
    audio_dict = await async_retrieve_data(filepath_hash)
    if audio_dict is not None:
        cached_path = None
        # Old cache format?
        if AUDIO_PATH_KEY not in audio_dict:
            audio_dict = {AUDIO_PATH_KEY: audio_dict, AUDIO_DURATION_KEY: None}
        cached_path = audio_dict[AUDIO_PATH_KEY]

        # Validate Path
        if cached_path is not None and os.path.exists(str(cached_path)):
            if audio_dict[AUDIO_DURATION_KEY] is None:
                # Add duration data if audio_dict is old format
                audio = await async_get_audio_from_path(hass=hass,
                                                        filepath=cached_path,
                                                        cache=True)
                if audio is not None:
                    audio_dict[AUDIO_DURATION_KEY] = float(len(audio) / 1000.0)
                    await async_store_data(hass, filepath_hash, audio_dict)

        return audio_dict

    _LOGGER.debug(" - Audio data not found in cache.")
    await async_remove_cached_audio_data(hass, filepath_hash, True, True)
    return None


async def async_remove_cached_audio_data(hass: HomeAssistant,
                                         filepath_hash: str,
                                         clear_chimes_cache: bool = False,
                                         clear_temp_tts_cache: bool = False,
                                         clear_www_tts_cache: bool = False):
    """Remove cached audio data from Chime TTS' cache and deletes audio filepath from filesystem."""
    audio_dict = await async_retrieve_data(filepath_hash)
    temp_chimes_path = _data[TEMP_CHIMES_PATH_KEY]
    temp_path = _data[TEMP_PATH_KEY]
    public_path = _data[WWW_PATH_KEY]

    if audio_dict is not None:
        # Old cache format?
        if AUDIO_PATH_KEY not in audio_dict:
            audio_dict = {AUDIO_PATH_KEY: audio_dict}

        cached_path = audio_dict[AUDIO_PATH_KEY]
        if cached_path and os.path.exists(cached_path):

            # Stop if user wishes to keep chime file
            if temp_chimes_path in cached_path and clear_chimes_cache is False:
                return
            # Stop if user wishes to keep temp file
            if temp_path in cached_path and clear_temp_tts_cache is False:
                return
            # Stop if user wishes to keep public file
            if public_path in cached_path and clear_www_tts_cache is False:
                return

            os.remove(str(cached_path))
            if os.path.exists(cached_path):
                _LOGGER.warning(
                    " - Unable to delete cached file '%s'.", str(cached_path)
                )
            else:
                _LOGGER.debug(
                    " - Cached file '%s' deleted successfully.", str(cached_path)
                )
        else:
            _LOGGER.debug(" - Cached file '%s' not found.", str(cached_path))
        _data[DATA_STORAGE_KEY].pop(filepath_hash)

        await async_save_data(hass)
    else:
        _LOGGER.debug(
            " - filepath_hash %s does not exist in the cache.", str(filepath_hash)
        )





def get_filename_hash_from_service_data(params: dict, options: dict):
    """Generate a hash from a unique string."""

    unique_string = ""
    relevant_params = [
        "message",
        "tts_platform",
        "gender",
        "tld",
        "voice",
        "language",
        "chime_path",
        "end_chime_path",
        "offset",
        "tts_playback_speed",
    ]
    for param in relevant_params:
        for dictionary in [params, options]:
            if (
                param in dictionary
                and dictionary[param] is not None
                and len(str(dictionary[param])) > 0
            ):
                unique_string = unique_string + "-" + str(dictionary[param])

    hash_value = helpers.get_hash_for_string(unique_string)
    return hash_value



#######################
# Integration options #
#######################

async def async_options(self, entry: ConfigEntry):
    """Present current configuration options for modification."""
    # Create an options flow handler and return it
    return ChimeTTSOptionsFlowHandler(entry)


async def async_options_updated(self, entry: ConfigEntry):
    """Handle updated configuration options and update the entry."""
    # Update the queue timeout value
    update_configuration(entry, None)


def update_configuration(config_entry: ConfigEntry, hass: HomeAssistant = None):
    """Update configurable values."""

    # Prepare default paths
    if hass is not None:
        _data[ROOT_PATH_KEY] = hass.config.path("").replace("/config", "")

    if DEFAULT_TEMP_PATH_KEY not in _data:
        _data[DEFAULT_TEMP_PATH_KEY] = hass.config.path(TEMP_PATH_DEFAULT)

    if DEFAULT_TEMP_CHIMES_PATH_KEY not in _data:
        _data[DEFAULT_TEMP_CHIMES_PATH_KEY] = hass.config.path(TEMP_CHIMES_PATH_DEFAULT)

    if DEFAULT_WWW_PATH_KEY not in _data:
        _data[DEFAULT_WWW_PATH_KEY] = hass.config.path(WWW_PATH_DEFAULT)

    # Set configurable values
    options = config_entry.options

    # Queue timeout
    _data[QUEUE_TIMEOUT_KEY] = options.get(QUEUE_TIMEOUT_KEY, QUEUE_TIMEOUT_DEFAULT)

    # Media folder (default local)
    _data[MEDIA_DIR_KEY] = options.get(MEDIA_DIR_KEY, MEDIA_DIR_DEFAULT)

    # www / local folder path
    _data[WWW_PATH_KEY] = hass.config.path(
        options.get(WWW_PATH_KEY, WWW_PATH_DEFAULT)
    )
    _data[WWW_PATH_KEY] = (_data[WWW_PATH_KEY] + "/").replace("//", "/")

    # Temp chimes folder path
    _data[TEMP_CHIMES_PATH_KEY] = hass.config.path(
        options.get(TEMP_CHIMES_PATH_KEY, _data[DEFAULT_TEMP_CHIMES_PATH_KEY])
    )
    _data[TEMP_CHIMES_PATH_KEY] = (_data[TEMP_CHIMES_PATH_KEY] + "/").replace("//", "/")

    # Temp folder path
    _data[TEMP_PATH_KEY] = hass.config.path(
        options.get(TEMP_PATH_KEY, _data[DEFAULT_TEMP_PATH_KEY])
    )
    _data[TEMP_PATH_KEY] = (_data[TEMP_PATH_KEY] + "/").replace("//", "/")

    # Custom chime paths
    _data[MP3_PRESET_CUSTOM_KEY] = {}
    for i in range(5):
        key = MP3_PRESET_CUSTOM_PREFIX + str(i + 1)
        value = options.get(key, "")
        _data[MP3_PRESET_CUSTOM_KEY][key] = value

    # Debug summary
    for key_string in [
        QUEUE_TIMEOUT_KEY,
        TEMP_CHIMES_PATH_KEY,
        TEMP_PATH_KEY,
        WWW_PATH_KEY,
        MEDIA_DIR_KEY,
        MP3_PRESET_CUSTOM_KEY,
    ]:
        _LOGGER.debug("%s = %s", key_string, str(_data[key_string]))
