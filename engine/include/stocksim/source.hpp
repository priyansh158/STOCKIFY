// Event sources (A2 §2): where the engine's input records come from.
//
//   PackSource      one or many validated packs, merged by (t, rank, pack index, seq) (A4 §1.3);
//   VectorSource    records held in memory (tests);
//   LiveBatchSource encoded records pushed in batches from Python (paper sessions, M5).
//
// next() returns a pointer to the next event, valid until the following call, or nullptr at the end (for a
// live source: at the end of what has been pushed so far).
#pragma once

#include <cstddef>
#include <deque>
#include <memory>
#include <optional>
#include <span>
#include <string>
#include <vector>

#include "stocksim/event.hpp"
#include "stocksim/pack_reader.hpp"

namespace stocksim {

class EventSource {
 public:
    virtual ~EventSource() = default;
    virtual const Event* next() = 0;
    virtual std::optional<EventKey> peek_key() const = 0;
};

class PackSource : public EventSource {
 public:
    // The pack's src is the number of packs added before it (its index in the bundle's pack list).
    void add(std::shared_ptr<const PackReader> pack);
    const Event* next() override;
    std::optional<EventKey> peek_key() const override;
    std::size_t packs() const { return cursors_.size(); }

 private:
    struct Cursor {
        std::shared_ptr<const PackReader> pack;
        std::size_t offset = kHeaderSize;
        Event head;
        bool has_head = false;
        void advance(std::uint32_t src);
    };
    std::ptrdiff_t smallest() const;
    std::vector<Cursor> cursors_;
    Event current_;
};

class VectorSource : public EventSource {
 public:
    // Each string is one encoded record (prefix included). Events are served in key order.
    explicit VectorSource(std::vector<std::string> records, std::uint32_t src = 0);
    // Its events point into its own strings, so a copy would point into the original's (M3 review).
    VectorSource(const VectorSource&) = delete;
    VectorSource& operator=(const VectorSource&) = delete;
    const Event* next() override;
    std::optional<EventKey> peek_key() const override;

 private:
    std::vector<std::string> records_;
    std::vector<Event> events_;
    std::size_t pos_ = 0;
};

class LiveBatchSource : public EventSource {
 public:
    // A batch is whole encoded records with no header; each must sort at or after the previous one.
    void push(std::span<const std::byte> batch);
    const Event* next() override;
    std::optional<EventKey> peek_key() const override;
    std::size_t pending() const;

 private:
    std::deque<std::string> batches_;  // the batch being read is batches_.front()
    std::size_t offset_ = 0;
    Event current_;
    bool have_last_ = false;
    EventKey last_;
};

}  // namespace stocksim
