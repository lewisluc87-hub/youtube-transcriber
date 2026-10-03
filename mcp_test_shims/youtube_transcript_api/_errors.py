class CouldNotRetrieveTranscript(Exception):
    pass


class NoTranscriptFound(CouldNotRetrieveTranscript):
    def __init__(self, video_id, requested_language_codes, transcript_list):
        self.video_id = video_id
        self.requested_language_codes = requested_language_codes
        super().__init__(f"No transcript found for {video_id!r} in {requested_language_codes!r}")


class TranscriptsDisabled(CouldNotRetrieveTranscript):
    def __init__(self, video_id):
        self.video_id = video_id
        super().__init__(f"Transcripts disabled for {video_id!r}")


class VideoUnavailable(CouldNotRetrieveTranscript):
    def __init__(self, video_id):
        self.video_id = video_id
        super().__init__(f"Video unavailable: {video_id!r}")
