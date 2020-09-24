(define (domain nightout)
    (:requirements :typing)
    ;(:types person location machine)
    (:constants @min @max @amount)
    (:predicates
        (person ?p)
        (machine ?m)
        (pcash ?c)
        (wcash ?c)
        (mcash ?c)

        (ge ?c1 ?c2)
        (sum ?c1 ?c2 ?c3)

        (finished)
        (inpocket ?p ?c)
        (maxwithdraw ?m ?c)
    )
    (:functions
        ;(minwithdraw ?m - machine)
    )

    ; TODO: convert typing using diverse code
    (:action withdraw_money
     :parameters (?p ?m ?wcash ?pcash1 ?pcash2 ?mcash1 ?mcash2)
     :precondition (and (person ?p) (machine ?m)
                        ; (ge ?wcash @min) (ge @max ?wcash) (ge ?mcash1 ?wcash)
                        (sum ?pcash1 ?wcash ?pcash2) (sum ?mcash2 ?wcash ?mcash1) ; TODO: separate
                        (inpocket ?p ?pcash1) (maxwithdraw ?m ?mcash1))
     :effect (and (inpocket ?p ?pcash2) (maxwithdraw ?m ?mcash2)
                  (not (inpocket ?p ?pcash1)) (not (maxwithdraw ?m ?mcash1))
                  (increase (total-cost) 2)))

    (:action finish
     :parameters (?p ?wcash)
     :precondition (and (person ?p) (ge ?wcash @amount) ; (pcash ?wcash)
                        (inpocket ?p ?wcash))
     :effect (finished))
)
; https://github.com/Emresav/ECAI16Domains/blob/master/cashpoint/domain0.pddl
