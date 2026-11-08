#include "stocksim/source.hpp"

#include <algorithm>
#include <string>

#include "stocksim/errors.hpp"

namespace stocksim {

// ── PackSource ───────────────────────────────────────────────────────────────

void PackSource::Cursor::advance(std::uint32_t src) {
    if (offset >= pack->length()) {
        has_head = false;
        return;
    }
    head = read_record(pack->data(), pack->length(), offset, src);
    offset += head.size;
    has_head = true;
}

void PackSource::add(std::shared_ptr<const PackReader> pack) {
    Cursor c;
    c.pack = std::move(pack);
    c.advance(static_cast<std::uint32_t>(cursors_.size()));
    cursors_.push_back(std::move(c));
}

std::ptrdiff_t PackSource::smallest() const {
    std::ptrdiff_t best = -1;
    for (std::size_t i = 0; i < cursors_.size(); ++i) {
        if (cursors_[i].has_head &&
            (best < 0 || cursors_[i].head.key() < cursors_[static_cast<std::size_t>(best)].head.key())) {
            best = static_cast<std::ptrdiff_t>(i);
        }
    }
    return best;
}

const Event* PackSource::next() {
    const std::ptrdiff_t i = smallest();
    if (i < 0) {
        return nullptr;
    }
    Cursor& c = cursors_[static_cast<std::size_t>(i)];
    current_ = c.head;
    c.advance(static_cast<std::uint32_t>(i));
    return &current_;
}

std::optional<EventKey> PackSource::peek_key() const {
    const std::ptrdiff_t i = smallest();
    if (i < 0) {
        return std::nullopt;
    }
    return cursors_[static_cast<std::size_t>(i)].head.key();
}

// ── VectorSource ─────────────────────────────────────────────────────────────

VectorSource::VectorSource(std::vector<std::string> records, std::uint32_t src) : records_(std::move(records)) {
    events_.reserve(records_.size());
    for (std::size_t i = 0; i < records_.size(); ++i) {
        const std::string& r = records_[i];
        const Event e = read_record(reinterpret_cast<const std::byte*>(r.data()), r.size(), 0, src);
        if (e.size != r.size()) {
            throw FormatError("record " + std::to_string(i) + " has " + std::to_string(r.size() - e.size) +
                              " trailing bytes");
        }
        events_.push_back(e);
    }
    std::stable_sort(events_.begin(), events_.end(), [](const Event& a, const Event& b) { return a.key() < b.key(); });
}

const Event* VectorSource::next() { return pos_ < events_.size() ? &events_[pos_++] : nullptr; }

std::optional<EventKey> VectorSource::peek_key() const {
    if (pos_ >= events_.size()) {
        return std::nullopt;
    }
    return events_[pos_].key();
}

// ── LiveBatchSource ──────────────────────────────────────────────────────────

void LiveBatchSource::push(std::span<const std::byte> batch) {
    // Check the whole batch first, against a local copy of the ordering mark: a batch that fails part-way is
    // refused whole and leaves the mark where it was (M3 review).
    std::string copy(reinterpret_cast<const char*>(batch.data()), batch.size());
    bool have = have_last_;
    EventKey last = last_;
    std::size_t off = 0;
    while (off < copy.size()) {
        const Event e = read_record(reinterpret_cast<const std::byte*>(copy.data()), copy.size(), off, 0);
        const EventKey key = e.key();
        if (have && key < last) {
            throw FormatError("pushed record out of order at batch byte " + std::to_string(off) + " (seq " +
                              std::to_string(e.seq) + ")");
        }
        last = key;
        have = true;
        off += e.size;
    }
    // Drop only batches that have been read to the end (the caller has finished with any event returned from
    // them); a batch not yet read, or read half-way, stays.
    while (!batches_.empty() && offset_ >= batches_.front().size()) {
        batches_.pop_front();
        offset_ = 0;
    }
    last_ = last;
    have_last_ = have;
    if (!copy.empty()) {
        batches_.push_back(std::move(copy));
    }
}

const Event* LiveBatchSource::next() {
    while (!batches_.empty() && offset_ >= batches_.front().size()) {
        if (batches_.size() == 1) {
            return nullptr;  // keep the last batch alive until the next push: the caller may still hold its event
        }
        batches_.pop_front();
        offset_ = 0;
    }
    if (batches_.empty()) {
        return nullptr;
    }
    const std::string& b = batches_.front();
    current_ = read_record(reinterpret_cast<const std::byte*>(b.data()), b.size(), offset_, 0);
    offset_ += current_.size;
    return &current_;
}

std::optional<EventKey> LiveBatchSource::peek_key() const {
    std::size_t idx = 0;
    std::size_t off = offset_;
    while (idx < batches_.size() && off >= batches_[idx].size()) {
        ++idx;
        off = 0;
    }
    if (idx >= batches_.size()) {
        return std::nullopt;
    }
    const std::string& b = batches_[idx];
    return read_record(reinterpret_cast<const std::byte*>(b.data()), b.size(), off, 0).key();
}

std::size_t LiveBatchSource::pending() const {
    std::size_t n = 0;
    std::size_t off = offset_;
    for (const std::string& b : batches_) {
        while (off < b.size()) {
            off += read_record(reinterpret_cast<const std::byte*>(b.data()), b.size(), off, 0).size;
            ++n;
        }
        off = 0;
    }
    return n;
}

}  // namespace stocksim
