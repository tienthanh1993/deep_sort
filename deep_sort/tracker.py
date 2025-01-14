# vim: expandtab:ts=4:sw=4
from __future__ import absolute_import

import math
import numpy as np
from scipy import spatial

from . import iou_matching
from . import kalman_filter
from . import linear_assignment
from .track import Track


def calculate_cosine_distance(a, b):
    cosine_distance = float(spatial.distance.cosine(a, b))
    return cosine_distance


def calculate_cosine_similarity(a, b):
    cosine_similarity = 1 - calculate_cosine_distance(a, b)
    return cosine_similarity


def calculate_angular_distance(a, b):
    cosine_similarity = calculate_cosine_similarity(a, b)
    angular_distance = math.acos(cosine_similarity) / math.pi
    return angular_distance


def calculate_angular_similarity(a, b):
    angular_similarity = 1 - calculate_angular_distance(a, b)
    return angular_similarity


class Tracker:
    """
    This is the multi-target tracker.

    Parameters
    ----------
    metric : nn_matching.NearestNeighborDistanceMetric
        A distance metric for measurement-to-track association.
    max_age : int
        Maximum number of missed misses before a track is deleted.
    n_init : int
        Number of consecutive detections before the track is confirmed. The
        track state is set to `Deleted` if a miss occurs within the first
        `n_init` frames.

    Attributes
    ----------
    metric : nn_matching.NearestNeighborDistanceMetric
        The distance metric used for measurement to track association.

    on_track_add : object
        callback when detect new track
    on_track_feature_add : object
        callback when detect new track
    max_age : int
        Maximum number of missed misses before a track is deleted.
    n_init : int
        Number of frames that a track remains in initialization phase.
    kf : kalman_filter.KalmanFilter
        A Kalman filter to filter target trajectories in image space.
    tracks : List[Track]
        The list of active tracks at the current time step.


    """

    def __init__(self, metric, on_track_add=None, on_track_feature_add=None, max_iou_distance=0.7, max_age=30,
                 n_init=3):
        self.metric = metric
        self.max_iou_distance = max_iou_distance
        self.max_age = max_age
        self.n_init = n_init

        self.kf = kalman_filter.KalmanFilter()
        self.tracks = []
        self._next_id = 1
        self.on_track_add = on_track_add
        self.on_track_feature_add = on_track_feature_add

    def predict(self):
        """Propagate track state distributions one time step forward.

        This function should be called once every time step, before `update`.
        """
        for track in self.tracks:
            track.predict(self.kf)

    def cosine_similarity(self, a, b):
        """

        :param b:
        :return:
        """
        return sum([i * j for i, j in zip(a, b)]) / (
                math.sqrt(sum([i * i for i in a])) * math.sqrt(sum([i * i for i in b])))

    def update(self, detections, video="", frame_id=0, frame=None):
        """Perform measurement update and track management.

        Parameters
        ----------
        detections : List[deep_sort.detection.Detection]
            A list of detections at the current time step.
        video : str
            current video path
        frame_id : int
            current frame id
        frame : bytearray
            current frame image

        """
        # Run matching cascade.
        matches, unmatched_tracks, unmatched_detections = \
            self._match(detections)

        # Update track set.
        for track_idx, detection_idx in matches:
            self.tracks[track_idx].update(self.kf, detections[detection_idx])
            if self.on_track_feature_add is not None:
                #  confirmed feature
                if self.tracks[track_idx].hits % (self.n_init * 3) == 0:
                    bbox = detections[detection_idx].to_tlbr()
                    crop_img = frame[int(bbox[1]):int(bbox[3]), int(bbox[0]):int(bbox[2])]
                    self.on_track_feature_add(video, frame_id, crop_img.copy(),
                                              bbox,
                                              self.tracks[track_idx].track_id,
                                              self.tracks[track_idx].features[-1],
                                              detections[detection_idx].confidence)

        for track_idx in unmatched_tracks:
            self.tracks[track_idx].mark_missed()
        for detection_idx in unmatched_detections:
            # for track in self.tracks:
            #     print("newid %d track_id=%d feature distance %f " % (
            #         self._next_id, track.track_id, calculate_cosine_similarity(track.last_detection.feature,
            #                                                                  detections[detection_idx].feature)))

            self._initiate_track(detections[detection_idx])
            if self.on_track_add is not None:
                self.on_track_add(video, frame_id, frame, self.tracks[- 1].track_id, self.tracks[- 1].class_name)

            if self.on_track_feature_add is not None:  # first seen feature
                bbox = detections[detection_idx].to_tlbr()
                crop_img = frame[int(bbox[1]):int(bbox[3]), int(bbox[0]):int(bbox[2])]
                self.on_track_feature_add(video, frame_id, crop_img.copy(),
                                          bbox,
                                          self.tracks[- 1].track_id,
                                          detections[detection_idx].feature,
                                          detections[detection_idx].confidence)

        self.tracks = [t for t in self.tracks if not t.is_deleted()]

        # Update distance metric.
        active_targets = [t.track_id for t in self.tracks if t.is_confirmed()]
        features, targets = [], []
        for track in self.tracks:
            if not track.is_confirmed():
                continue
            features += track.features
            targets += [track.track_id for _ in track.features]
            track.features = []
        self.metric.partial_fit(
            np.asarray(features), np.asarray(targets), active_targets)

    def _match(self, detections):

        def gated_metric(tracks, dets, track_indices, detection_indices):
            features = np.array([dets[i].feature for i in detection_indices])
            targets = np.array([tracks[i].track_id for i in track_indices])
            cost_matrix = self.metric.distance(features, targets)
            cost_matrix = linear_assignment.gate_cost_matrix(
                self.kf, cost_matrix, tracks, dets, track_indices,
                detection_indices)
            return cost_matrix

        # Split track set into confirmed and unconfirmed tracks.
        confirmed_tracks = [
            i for i, t in enumerate(self.tracks) if t.is_confirmed()]
        unconfirmed_tracks = [
            i for i, t in enumerate(self.tracks) if not t.is_confirmed()]

        # Associate confirmed tracks using appearance features.
        matches_a, unmatched_tracks_a, unmatched_detections = \
            linear_assignment.matching_cascade(
                gated_metric, self.metric.matching_threshold, self.max_age,
                self.tracks, detections, confirmed_tracks)

        # Associate remaining tracks together with unconfirmed tracks using IOU.
        iou_track_candidates = unconfirmed_tracks + [
            k for k in unmatched_tracks_a if
            self.tracks[k].time_since_update == 1]
        unmatched_tracks_a = [
            k for k in unmatched_tracks_a if
            self.tracks[k].time_since_update != 1]
        matches_b, unmatched_tracks_b, unmatched_detections = \
            linear_assignment.min_cost_matching(
                iou_matching.iou_cost, self.max_iou_distance, self.tracks,
                detections, iou_track_candidates, unmatched_detections)

        matches = matches_a + matches_b
        unmatched_tracks = list(set(unmatched_tracks_a + unmatched_tracks_b))
        return matches, unmatched_tracks, unmatched_detections

    def _initiate_track(self, detection):
        mean, covariance = self.kf.initiate(detection.to_xyah())
        class_name = detection.get_class()
        self.tracks.append(Track(
            mean, covariance, self._next_id, self.n_init, self.max_age,
            detection.feature, class_name, detection))
        self._next_id += 1
