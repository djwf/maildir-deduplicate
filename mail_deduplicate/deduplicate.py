# -*- coding: utf-8 -*-
#
# Copyright Kevin Deldycke <kevin@deldycke.com> and contributors.
# All Rights Reserved.
#
# This program is Free Software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.

from collections import Counter
from difflib import unified_diff
from itertools import combinations
from operator import attrgetter
from pathlib import Path

import click
from boltons.cacheutils import cachedproperty
from boltons.iterutils import unique
from tabulate import tabulate

from . import ContentDiffAboveThreshold, SizeDiffAboveThreshold, TooFewHeaders, logger
from .colorize import choice_style, subtitle_style
from .mailbox import open_box
from .strategy import apply_strategy


class DuplicateSet:

    """A duplicate set of mails sharing the same hash.

    Implements all selection strategies applicable to a set of duplicate mails.
    """

    def __init__(self, hash_key, mail_set, conf):
        """Load-up the duplicate set of mail and freeze pool.

        Once loaded-up, the pool of parsed mails is considered frozen for the
        rest of the duplicate set's life. This allow aggressive caching of lazy
        instance attributes depending on the pool content.
        """
        self.hash_key = hash_key

        # Global config.
        self.conf = conf

        # Pool referencing all duplicated mails and their attributes.
        self.pool = frozenset(mail_set)

        # Keep set metrics.
        self.stats = Counter()

        logger.debug(f"{self!r} created.")

    def __repr__(self):
        """ Print internal raw states for debugging. """
        return f"<{self.__class__.__name__} hash={self.hash_key} size={self.size}>"

    @cachedproperty
    def size(self):
        """ Return the size of the duplicate set. """
        return len(self.pool)

    @cachedproperty
    def newest_timestamp(self):
        return max(map(attrgetter("timestamp"), self.pool))

    @cachedproperty
    def oldest_timestamp(self):
        return min(map(attrgetter("timestamp"), self.pool))

    @cachedproperty
    def biggest_size(self):
        return max(map(attrgetter("size"), self.pool))

    @cachedproperty
    def smallest_size(self):
        return min(map(attrgetter("size"), self.pool))

    def check_differences(self):
        """Ensures all mail differs in the limits imposed by size and content
        thresholds.

        Compare all mails of the duplicate set with each other, both in size
        and content. Raise an error if we're not within the limits imposed by
        the threshold settings.
        """
        logger.info("Check mail differences are below the thresholds.")
        if self.conf.size_threshold < 0:
            logger.info("Skip checking for size differences.")
        if self.conf.content_threshold < 0:
            logger.info("Skip checking for content differences.")
        if self.conf.size_threshold < 0 and self.conf.content_threshold < 0:
            return

        # Compute differences of mail against one another.
        for mail_a, mail_b in combinations(self.pool, 2):

            # Compare mails on size.
            if self.conf.size_threshold > -1:
                size_difference = abs(mail_a.size - mail_b.size)
                logger.debug(
                    f"{mail_a!r} and {mail_b!r} differs by {size_difference} bytes in size."
                )
                if size_difference > self.conf.size_threshold:
                    raise SizeDiffAboveThreshold

            # Compare mails on content.
            if self.conf.content_threshold > -1:
                content_difference = self.diff(mail_a, mail_b)
                logger.debug(
                    f"{mail_a!r} and {mail_b!r} differs by {content_difference} bytes in "
                    "content."
                )
                if content_difference > self.conf.content_threshold:
                    if self.conf.show_diff:
                        logger.info(self.pretty_diff(mail_a, mail_b))
                    raise ContentDiffAboveThreshold

    def diff(self, mail_a, mail_b):
        """Return difference in bytes between two mails' normalized body.

        TODO: rewrite the diff algorithm to not rely on naive unified diff
        result parsing.
        """
        return len(
            "".join(
                unified_diff(
                    mail_a.body_lines,
                    mail_b.body_lines,
                    # Ignore difference in filename lengths and timestamps.
                    fromfile="a",
                    tofile="b",
                    fromfiledate="",
                    tofiledate="",
                    n=0,
                    lineterm="\n",
                )
            )
        )

    def pretty_diff(self, mail_a, mail_b):
        """Returns a verbose unified diff between two mails' normalized body."""
        return "".join(
            unified_diff(
                mail_a.body_lines,
                mail_b.body_lines,
                fromfile=f"Normalized body of {mail_a.path}",
                tofile=f"Normalized body of {mail_b.path}",
                fromfiledate=f"{mail_a.timestamp:0.2f}",
                tofiledate=f"{mail_b.timestamp:0.2f}",
                n=0,
                lineterm="\n",
            )
        )

    def select_candidates(self):
        """Returns the list of duplicates selected for removal.

        Run preliminary checks and return the candidates fitting the strategy
        and constraints set by the configuration."""
        if self.size == 1:
            logger.debug("Ignore set: only one message found.")
            self.stats["mail_unique"] += self.size
            self.stats["mail_discarded"] += self.size
            self.stats["set_ignored"] += 1
            return

        self.stats["mail_duplicates"] += self.size

        # Fine-grained checks on mail differences.
        try:
            self.check_differences()
        except UnicodeDecodeError as expt:
            self.stats["mail_discarded"] += self.size
            self.stats["set_rejected_encoding"] += 1
            logger.warning("Reject set: unparseable mails due to bad encoding.")
            logger.debug(f"{expt}")
            return
        except SizeDiffAboveThreshold:
            self.stats["mail_discarded"] += self.size
            self.stats["set_rejected_size"] += 1
            logger.warning("Reject set: mails are too dissimilar in size.")
            return
        except ContentDiffAboveThreshold:
            self.stats["mail_discarded"] += self.size
            self.stats["set_rejected_content"] += 1
            logger.warning("Reject set: mails are too dissimilar in content.")
            return

        if not self.conf.strategy:
            logger.warning("No strategy selected, skip selection.")
            self.stats["mail_discarded"] += self.size
            self.stats["set_skipped"] += 1
            return

        # Fetch the subset of selected mails from the set by applying the
        selected_uids = apply_strategy(self.conf.strategy, self)

        # Duplicate sets matching as a whole are skipped altogether.
        candidate_count = len(selected_uids)
        if candidate_count == self.size:
            logger.warning(
                f"Skip whole set, all {candidate_count} mails within were selected. "
                "The strategy criterion was not able to discard some."
            )
            self.stats["mail_discarded"] += candidate_count
            self.stats["set_skipped"] += 1
            return

        logger.info(f"{candidate_count} mail candidates selected for action.")
        self.stats["mail_selected"] += candidate_count
        self.stats["mail_discarded"] += self.size - candidate_count
        self.stats["set_deduplicated"] += 1
        return selected_uids


class Deduplicate:

    """Load-up messages, search for duplicates, apply selection strategy and perform
    the action.

    Similar messages sharing the same hash are grouped together in a ``DuplicateSet``.
    """

    def __init__(self, conf):
        # Index of mail sources by their full, normalized path. So we can refer
        # to them in Mail instances. Also have the nice side effect of natural
        # deduplication of sources themselves.
        self.sources = {}

        # All mails grouped by hashes.
        self.mails = {}

        # List of mail's IDs selected after application of selection strategy.
        self.selection = []

        # Global config.
        self.conf = conf

        # Deduplication statistics.
        self.stats = Counter(
            {
                # Total number of mails encountered from all mail sources.
                "mail_found": 0,
                # Number of mails ignored because they were faulty or unparseable.
                "mail_rejected": 0,
                # Number of valid mails parsed and retained for deduplication.
                "mail_retained": 0,
                # Number of unique mails (which ended up in duplicate sets with
                # one mail and one only).
                "mail_unique": 0,
                # Number of duplicate mails (sum of mails in all duplicate sets
                # with at least 2 mails).
                "mail_duplicates": 0,
                # Number of mails discarded from the final selection.
                "mail_discarded": 0,
                # Number of mails kept in the final selection.
                "mail_selected": 0,
                # Number of mails copied from their original mailbox to another.
                "mail_copied": 0,
                # Number of mails moved from their original mailbox to another.
                "mail_moved": 0,
                # Number of mails deleted from their mailbox in-place.
                "mail_deleted": 0,

                # Total number of duplicate sets.
                "set_total": 0,
                # Total number of unprocessed sets because mail is unique.
                "set_ignored": 0,
                # Total number of sets skipped as already deduplicated.
                "set_skipped": 0,
                # Number of sets ignored because they were faulty.
                "set_rejected_encoding": 0,
                "set_rejected_size": 0,
                "set_rejected_content": 0,
                # Number of valid sets successfuly deduplicated.
                "set_deduplicated": 0,
            }
        )

    def add_source(self, source_path):
        """Registers a source of mails, validates and opens it. """
        # Make the path absolute, resolving any symlinks. Do not allow duplicates in
        # our sources, as we use the path as a unique key to tie back a mail from its
        # source when performing the action later.
        source_path = Path(source_path).resolve(strict=True)
        if source_path in self.sources:
            raise ValueError(f"{source_path} already added.")

        # Open and register the mail source.
        box = open_box(source_path, self.conf.input_format, self.conf.force_unlock)
        self.sources[source_path] = box

        # Keep track of global mail count.
        mail_found = len(box)
        logger.info(f"{mail_found} mails found.")
        self.stats["mail_found"] += mail_found

    def hash_all(self):
        """Browse all mails from all registered sources, compute hashes and group mails
        by hash.

        Displays a progress bar as the operation might be slow.
        """
        logger.info(
            f"Use [{', '.join(map(choice_style, self.conf.hash_headers))}] headers to "
            "compute hashes.")

        with click.progressbar(
            length=self.stats["mail_found"],
            label="Hashed mails",
            show_pos=True,
        ) as progress:

            for source_path, box in self.sources.items():
                for mail_id, mail in box.iteritems():

                    # Re-attach box_path and mail_id to let the mail carry its
                    # own information on its origin box and index in this box.
                    mail.mail_id = mail_id
                    mail.source_path = source_path
                    mail.conf = self.conf

                    try:
                        mail_hash = mail.hash_key
                    except TooFewHeaders as expt:
                        logger.warning(f"Rejecting {mail.path}: {expt.args[0]}")
                        self.stats["mail_rejected"] += 1
                    else:
                        # Use a set to deduplicate entries pointing to the same file.
                        self.mails.setdefault(mail_hash, set()).add(mail)
                        self.stats["mail_retained"] += 1

                    progress.update(1)

    def select_all(self):
        """Gather the final selection of mails from each duplicate set.

        We apply the selection strategy one duplicate set at a time to keep memory
        footprint low and make the log easier to read.
        """
        if self.conf.strategy:
            logger.info(
                f"{choice_style(self.conf.strategy)} strategy will be applied on each "
                "duplicate set to select candidates."
            )
        else:
            logger.warning("No strategy configured, skip selection.")

        self.stats["set_total"] = len(self.mails)

        for hash_key, mail_set in self.mails.items():

            # Alter log level depending on set length.
            mail_count = len(mail_set)
            log_level = logger.debug if mail_count == 1 else logger.info
            log_level(subtitle_style(f"◼ {mail_count} mails sharing hash {hash_key}"))

            # Performs the selection within the set.
            duplicates = DuplicateSet(hash_key, mail_set, self.conf)
            candidates = duplicates.select_candidates()
            if candidates:
                self.selection += candidates

            # Merge duplicate set's stats to global stats.
            self.stats += duplicates.stats

        # Close all open boxes.
        for box in self.sources.values():
            box.close()

    def remove_selection(self):
        """Performs the action of removing the selected mail candidates
        in-place, from their original boxes."""
        # Check our indexing and selection methods are not flagging candidates
        # several times.
        assert unique(self.selection) == self.selection

        for box_path, mail_id in self.selection:
            # TODO: fetch mail path from Mail object instance directly.
            mail_path = f"{box_path}:{mail_id}"
            self.stats["mail_deleted"] += 1

            logger.debug(f"Deleting {mail_path!r} in-place...")

            if self.conf.dry_run:
                logger.warning(f"DRY RUN: Skip deletion of {mail_path!r}.")
            else:
                self.sources[box_path].remove(mail_id)
                logger.info(f"{mail_path} deleted.")

    def report(self):
        """ Returns a text report of user-friendly statistics and metrics. """
        table = [
            ["Mails", "Metric"],
            ["Found", self.stats["mail_found"]],
            ["Rejected", self.stats["mail_rejected"]],
            ["Retained", self.stats["mail_retained"]],
            ["Unique", self.stats["mail_unique"]],
            ["Duplicates", self.stats["mail_duplicates"]],
            ["Discarded", self.stats["mail_discarded"]],
            ["Selected", self.stats["mail_selected"]],
            ["Copied", self.stats["mail_copied"]],
            ["Moved", self.stats["mail_moved"]],
            ["Deleted", self.stats["mail_deleted"]],
        ]
        output = tabulate(table, tablefmt="fancy_grid", headers="firstrow")

        table = [
            ["Duplicate sets", "Metric"],
            ["Total", self.stats["set_total"]],
            ["Ignored", self.stats["set_ignored"]],
            ["Skipped", self.stats["set_skipped"]],
            ["Rejected (bad encoding)", self.stats["set_rejected_encoding"]],
            ["Rejected (too dissimilar in size)", self.stats["set_rejected_size"]],
            [
                "Rejected (too dissimilar in content)",
                self.stats["set_rejected_content"],
            ],
            ["Deduplicated", self.stats["set_deduplicated"]],
        ]
        output += "\n"
        output += tabulate(table, tablefmt="fancy_grid", headers="firstrow")

        return output

    def check_stats(self):
        """Perform some high-level consistency checks on metrics.

        Helps users reports tricky edge-cases.
        """
        assert self.stats["mail_found"] >= self.stats["mail_rejected"]
        assert self.stats["mail_found"] >= self.stats["mail_retained"]
        assert self.stats["mail_found"] == (
            self.stats["mail_rejected"] + self.stats["mail_retained"]
        )

        assert self.stats["mail_retained"] >= self.stats["mail_unique"]
        assert self.stats["mail_retained"] >= self.stats["mail_duplicates"]
        assert self.stats["mail_retained"] == (
            self.stats["mail_unique"] + self.stats["mail_duplicates"]
        )

        assert self.stats["mail_retained"] == (
            self.stats["mail_discarded"] + self.stats["mail_selected"]
        )

        assert self.stats["mail_selected"] == (
            self.stats["mail_copied"] + self.stats["mail_moved"] +
            self.stats["mail_deleted"]
        )

        assert self.stats["mail_retained"] >= self.stats["mail_deleted"]
        assert self.stats["mail_duplicates"] == 0 or (
            self.stats["mail_duplicates"] > self.stats["mail_deleted"]
        )

        assert self.stats["set_ignored"] == self.stats["mail_unique"]

        assert self.stats["set_total"] == (
            self.stats["set_ignored"]
            + self.stats["set_rejected_encoding"]
            + self.stats["set_rejected_size"]
            + self.stats["set_rejected_content"]
            + self.stats["set_skipped"]
            + self.stats["set_deduplicated"]
        )
