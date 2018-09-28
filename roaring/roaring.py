# Copyright 2018 Pilosa Corp.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# 1. Redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright
# notice, this list of conditions and the following disclaimer in the
# documentation and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its
# contributors may be used to endorse or promote products derived
# from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND
# CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES,
# INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH
# DAMAGE.
#

from __future__ import division

__all__ = "Bitmap"

import array
import bisect
import copy
import io
import struct

MAGIC_NUMBER = 12348
STORAGE_VERSION = 0
COOKIE = MAGIC_NUMBER + (STORAGE_VERSION << 16)
HEADER_BASE_SIZE = 8
ARRAY_MAX_SIZE = 4096
BITMAP_N = (1 << 16) // 64
RUN_MAX_SIZE = 2048


class Container(object):

    __slots__ = "bitmap", "n"

    TYPE_ARRAY = 1
    TYPE_BITMAP = 2
    TYPE_RLE = 3

    def __init__(self):
        self.bitmap = [0] * BITMAP_N
        self.n = 0

    def add(self, bit):
        if (self.bitmap[bit // 64] & (1 << (bit % 64))):
            return
        self.n += 1
        self.bitmap[bit // 64] |= (1 << (bit % 64))

    def __iter__(self):
        power_range = range(64)
        for key, value in enumerate(self.bitmap):
            if not value:
                continue
            for i in power_range:
                v = 1 << i
                if value & v == v:
                    yield key * 64 + i

    def __lt__(self, other):
        # required for Python 3
        return False

    def __len__(self):
        return self.n

    def _optimal_serialization_type(self):
        arr_cost = 2 * self.n
        bitmap_cost = 8 * len(self.bitmap)
        rc = run_count(self.__iter__())
        if rc > RUN_MAX_SIZE:
            return self.TYPE_ARRAY if arr_cost < bitmap_cost else self.TYPE_BITMAP
        rle_cost = 2 + 4 * rc
        costs = [
            (arr_cost, self.TYPE_ARRAY),
            (bitmap_cost, self.TYPE_BITMAP),
            (rle_cost, self.TYPE_RLE)
        ]
        costs.sort()
        _, ser_type = costs[0]
        return ser_type

    def write_to(self, writer):
        ser_type = self._optimal_serialization_type()
        if ser_type == self.TYPE_ARRAY:
            arr = array.array("H", self.__iter__())
            return ser_type, writer.write(arr.tostring())
        elif ser_type == self.TYPE_BITMAP:
            ba = bytearray(8 * len(self.bitmap))
            for i, item in enumerate(self.bitmap):
                struct.pack_into("<Q", ba, i * 8, item)
            return ser_type, writer.write(ba)
        elif ser_type == self.TYPE_RLE:
            runs = to_runs(self.__iter__())
            written = writer.write(struct.pack("<H", len(runs)))
            for start, last in runs:
                written += writer.write(struct.pack("<HH", start, last))
            return ser_type, written
        else:
            raise Exception("Invalid container type: " % ser_type)


def to_runs(gen):
    runs = []
    try:
        start = last = next(gen)
    except StopIteration:
        return []
    for bit in gen:
        if bit == last + 1:
            last = bit
        else:
            runs.append((start, last))
            start = last = bit
    runs.append((start, last))
    return runs


def run_count(gen):
    count = 0
    try:
        last = next(gen)
    except StopIteration:
        return []
    for bit in gen:
        if bit == last + 1:
            last = bit
        else:
            count += 1
            last = bit
    count += 1
    return count


class Bitmap(object):

    __slots__ = "key_containers", "last_key", "last_container"
    _empty_container = Container()

    def __init__(self):
        self.key_containers = []
        self.last_key = 0
        self.last_container = None

    def add(self, bit):
        container = self._get_or_create(bit >> 16)
        container.add(bit & 0xFFFF)

    def __iter__(self):
        for key, container in self.key_containers:
            for bit in container:
                yield (key << 16) + bit

    def write_to(self, writer):
        # create the body
        container_meta = []
        data = io.BytesIO()
        for key, container in self.key_containers:
            # NOTE: since we don't support removing bits,
            # a container cannot be empty.
            type, size = container.write_to(data)
            container_meta.append((key, size, type, len(container)))

        container_count = len(container_meta)
        # write header
        writer.write(struct.pack("<I", COOKIE))
        writer.write(struct.pack("<I", container_count))

        # write container meta
        for key, size, type, bit_count in container_meta:
            writer.write(struct.pack("<Q", key))
            writer.write(struct.pack("<H", type))
            writer.write(struct.pack("<H", bit_count - 1))

        # write container data
        offset = HEADER_BASE_SIZE + container_count * (8 + 2 + 2 + 4)
        for key, size, type, bit_count in container_meta:
            writer.write(struct.pack("<I", offset))
            offset += size

        writer.write(data.getvalue())
        return offset

    def _put_container(self, key, container):
        bisect.insort(self.key_containers, (key, container))

    def _get_container(self, key):
        key_containers = self.key_containers
        index = bisect.bisect_left(key_containers, (key, self._empty_container))
        if index != len(key_containers):
            key2, container = key_containers[index]
            if key == key2:
                return container
        return None

    def _get_or_create(self, key):
        if key == self.last_key and self.last_container != None:
            return self.last_container
        self.last_key = key
        container = self._get_container(key)
        if not container:
            container = Container()
            self._put_container(key, container)
        self.last_container = container
        return container
